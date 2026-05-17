"""Tests for PR1 — Backend push Tier A.

Covers:
- S3 Transfer Acceleration env wiring in EchoService._get_s3_kwargs
- MIME allowlist on generate_upload_url
- CacheControl / Tagging / Metadata in the presigned PUT params
- update_echo rejects presigned URL writebacks
- finalize_upload: tenancy check, HeadObject success, missing-object, write
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.app.core.exceptions import NotFoundError, ValidationError
from src.app.models.echo import Echo, EchoStatus, EchoType
from src.app.services.echo_service import (
    ALLOWED_UPLOAD_MIME_TYPES,
    EchoService,
    _looks_like_presigned_url,
)

# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _ddb_resource_mock() -> tuple[MagicMock, AsyncMock]:
    """Build an aioboto3 DynamoDB-resource context manager and a Table mock."""
    table = AsyncMock()
    table.put_item = AsyncMock()
    resource = AsyncMock()
    resource.Table = AsyncMock(return_value=table)
    return resource, table


# ----------------------------------------------------------------------
# accelerate endpoint wiring
# ----------------------------------------------------------------------


def test_s3_kwargs_use_accelerate_when_env_truthy(monkeypatch):
    monkeypatch.setenv("S3_ACCELERATE_ENABLED", "true")
    svc = EchoService()
    cfg = svc._get_s3_kwargs()["config"]
    # botocore.config.Config exposes the merged values via `_user_provided_options`
    # internally, but the public surface is the Config.__eq__ shape — check the
    # s3 sub-key directly.
    assert cfg.s3 is not None and cfg.s3.get("use_accelerate_endpoint") is True


def test_s3_kwargs_no_accelerate_when_env_unset(monkeypatch):
    monkeypatch.delenv("S3_ACCELERATE_ENABLED", raising=False)
    svc = EchoService()
    cfg = svc._get_s3_kwargs()["config"]
    # Either no s3 sub-config, or accelerate explicitly false.
    s3_cfg = cfg.s3 or {}
    assert s3_cfg.get("use_accelerate_endpoint") in (None, False)


@pytest.mark.parametrize("val", ["false", "0", "no", "", "FALSE"])
def test_s3_kwargs_no_accelerate_for_falsey_values(monkeypatch, val):
    monkeypatch.setenv("S3_ACCELERATE_ENABLED", val)
    svc = EchoService()
    cfg = svc._get_s3_kwargs()["config"]
    s3_cfg = cfg.s3 or {}
    assert s3_cfg.get("use_accelerate_endpoint") in (None, False)


# ----------------------------------------------------------------------
# MIME allowlist + tagging / cache-control on presigned PUT
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_upload_url_rejects_unknown_mime():
    svc = EchoService()
    with pytest.raises(ValidationError, match="Unsupported media type"):
        await svc.generate_upload_url(
            user_id="u1",
            file_type="application/x-shockwave-flash",
            echo_id="e1",
        )


@pytest.mark.asyncio
async def test_generate_upload_url_signs_with_cache_control_and_tags():
    svc = EchoService()
    fake_s3 = AsyncMock()
    fake_s3.generate_presigned_url = AsyncMock(return_value="https://signed.example/")
    with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
        result = await svc.generate_upload_url(
            user_id="user-1",
            file_type="video/mp4",
            echo_id="echo-1",
            upload_type="echo",
        )

    args, kwargs = fake_s3.generate_presigned_url.call_args
    assert args[0] == "put_object"
    params = kwargs["Params"]
    assert params["ContentType"] == "video/mp4"
    assert params["CacheControl"] == "public, max-age=31536000, immutable"
    assert "user_id=user-1" in params["Tagging"]
    assert "echo_id=echo-1" in params["Tagging"]
    assert "upload_type=echo" in params["Tagging"]
    assert params["Metadata"]["user_id"] == "user-1"
    assert params["Metadata"]["echo_id"] == "echo-1"
    assert "signed_at" in params["Metadata"]

    # Result still carries the non-accelerated canonical URL for DDB.
    assert result["media_url"].startswith("https://")
    assert ".amazonaws.com/" in result["media_url"]
    assert result["upload_url"] == "https://signed.example/"


@pytest.mark.asyncio
async def test_generate_upload_url_accepts_all_allowlisted_types():
    """Every MIME on the allowlist signs without error."""
    svc = EchoService()
    fake_s3 = AsyncMock()
    fake_s3.generate_presigned_url = AsyncMock(return_value="signed")
    with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
        for mime in ALLOWED_UPLOAD_MIME_TYPES:
            await svc.generate_upload_url(user_id="u", file_type=mime, echo_id="e")
    # 11 distinct mime types in the allowlist.
    assert fake_s3.generate_presigned_url.await_count == len(ALLOWED_UPLOAD_MIME_TYPES)


# ----------------------------------------------------------------------
# update_echo presigned-URL guard (data-corruption fix)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://bucket.s3.us-east-1.amazonaws.com/key?X-Amz-Signature=deadbeef",
        "https://bucket.s3.us-east-1.amazonaws.com/key?X-Amz-Algorithm=AWS4-HMAC-SHA256",
        "https://bucket.s3.us-east-1.amazonaws.com/key?X-Amz-Expires=3600",
        "https://bucket.s3.us-east-1.amazonaws.com/key?X-Amz-Credential=AKIA",
        "https://bucket.s3.us-east-1.amazonaws.com/key?AWSAccessKeyId=AKIA&Signature=abc",
    ],
)
def test_looks_like_presigned_url_catches_known_forms(url):
    assert _looks_like_presigned_url(url) is True


def test_looks_like_presigned_url_passes_canonical():
    assert (
        _looks_like_presigned_url(
            "https://bucket.s3.us-east-1.amazonaws.com/echoes/u/e_1.mp4"
        )
        is False
    )


def test_looks_like_presigned_url_handles_none():
    assert _looks_like_presigned_url(None) is False
    assert _looks_like_presigned_url("") is False


@pytest.mark.asyncio
async def test_update_echo_rejects_presigned_media_url():
    draft = Echo(
        echo_id="echo-1",
        user_id="u-1",
        title="t",
        status=EchoStatus.DRAFT,
        media_url=None,
        recipient_id="r-1",
    )
    svc = EchoService()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=draft)):
        with pytest.raises(ValidationError, match="presigned URL"):
            await svc.update_echo(
                echo_id="echo-1",
                user_id="u-1",
                data={
                    "media_url": (
                        "https://bucket.s3.amazonaws.com/key?"
                        "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=x"
                    ),
                },
            )


@pytest.mark.asyncio
async def test_update_echo_accepts_canonical_media_url():
    draft = Echo(
        echo_id="echo-1",
        user_id="u-1",
        title="t",
        status=EchoStatus.DRAFT,
        media_url=None,
        recipient_id="r-1",
    )
    svc = EchoService()
    resource, table = _ddb_resource_mock()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=draft)):
        with patch.object(
            svc, "_get_dynamodb_resource", new=AsyncMock(return_value=resource)
        ):
            updated = await svc.update_echo(
                echo_id="echo-1",
                user_id="u-1",
                data={
                    "media_url": (
                        "https://bucket.s3.us-east-1.amazonaws.com/"
                        "echoes/u-1/echo-1_20260517.mp4"
                    ),
                },
            )

    assert updated.media_url == (
        "https://bucket.s3.us-east-1.amazonaws.com/echoes/u-1/echo-1_20260517.mp4"
    )
    table.put_item.assert_awaited_once()


# ----------------------------------------------------------------------
# finalize_upload
# ----------------------------------------------------------------------


def _make_echo(echo_id: str = "echo-1", user_id: str = "u-1") -> Echo:
    return Echo(
        echo_id=echo_id,
        user_id=user_id,
        title="t",
        status=EchoStatus.DRAFT,
        media_url=None,
        echo_type=EchoType.TEXT,
        recipient_id="r-1",
    )


@pytest.mark.asyncio
async def test_finalize_upload_rejects_cross_tenant_key():
    """Caller cannot bind an object outside their namespace to their echo."""
    svc = EchoService()
    echo = _make_echo()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(ValidationError, match="does not belong"):
            await svc.finalize_upload(
                echo_id="echo-1",
                user_id="u-1",
                key="echoes/another-user/echo-1_2026.mp4",
            )


@pytest.mark.asyncio
async def test_finalize_upload_rejects_cross_echo_key():
    """Caller cannot bind a key uploaded for echo-A to echo-B."""
    svc = EchoService()
    echo = _make_echo(echo_id="echo-B")
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(ValidationError, match="does not belong to this echo"):
            await svc.finalize_upload(
                echo_id="echo-B",
                user_id="u-1",
                key="echoes/u-1/echo-A_2026.mp4",  # uploaded for a different echo
            )


@pytest.mark.asyncio
async def test_finalize_upload_rejects_recipient_caller():
    """Recipient of an echo cannot finalize media on it — only owner can.

    get_echo returns the echo when called by the recipient (their inbox
    needs read access), so finalize_upload must do an explicit owner
    check on top of that. Returning NotFound (not Validation) avoids
    confirming the echo's existence to a non-owner.
    """
    svc = EchoService()
    # Echo owned by 'owner-u', not by the caller 'recipient-u'.
    echo = _make_echo(user_id="owner-u")
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(NotFoundError):
            await svc.finalize_upload(
                echo_id="echo-1",
                user_id="recipient-u",
                key="echoes/recipient-u/echo-1_2026.mp4",
            )


@pytest.mark.asyncio
async def test_finalize_upload_rejects_when_media_already_attached():
    """First-write semantics: re-finalize is not allowed."""
    svc = EchoService()
    echo = _make_echo()
    echo.media_url = "https://b.s3.us-east-1.amazonaws.com/echoes/u-1/old.mp4"
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(ValidationError, match="already been finalized"):
            await svc.finalize_upload(
                echo_id="echo-1",
                user_id="u-1",
                key="echoes/u-1/echo-1_2026.mp4",
            )


@pytest.mark.asyncio
async def test_finalize_upload_rejects_when_echo_missing():
    svc = EchoService()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=None)):
        with pytest.raises(NotFoundError):
            await svc.finalize_upload(
                echo_id="echo-1",
                user_id="u-1",
                key="echoes/u-1/echo-1_2026.mp4",
            )


@pytest.mark.asyncio
async def test_finalize_upload_rejects_when_object_missing_in_s3():
    svc = EchoService()
    echo = _make_echo()
    fake_s3 = AsyncMock()
    fake_s3.head_object = AsyncMock(
        side_effect=ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
    )
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            with pytest.raises(NotFoundError, match="No uploaded object"):
                await svc.finalize_upload(
                    echo_id="echo-1",
                    user_id="u-1",
                    key="echoes/u-1/echo-1_2026.mp4",
                )


@pytest.mark.asyncio
async def test_finalize_upload_persists_canonical_url_and_type():
    """HeadObject truth wins over caller's content_type hint."""
    svc = EchoService()
    svc.s3_bucket = "mc-bucket"
    svc.region = "us-east-1"

    echo = _make_echo()  # echo_type=TEXT initially
    fake_s3 = AsyncMock()
    fake_s3.head_object = AsyncMock(
        return_value={
            "ContentLength": 1234567,
            "ContentType": "video/mp4",
            "ETag": '"deadbeefcafe"',
        }
    )
    resource, table = _ddb_resource_mock()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            with patch.object(
                svc, "_get_dynamodb_resource", new=AsyncMock(return_value=resource)
            ):
                result = await svc.finalize_upload(
                    echo_id="echo-1",
                    user_id="u-1",
                    key="echoes/u-1/echo-1_2026.mp4",
                    content_type="image/jpeg",  # caller hint — should be IGNORED
                )

    assert result.media_url == (
        "https://mc-bucket.s3.us-east-1.amazonaws.com/echoes/u-1/echo-1_2026.mp4"
    )
    assert result.echo_type == EchoType.VIDEO  # from HEAD, not from caller hint
    table.put_item.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalize_upload_sets_audio_type_from_head():
    """An audio MIME from HEAD updates echo_type to AUDIO."""
    svc = EchoService()
    echo = _make_echo()
    fake_s3 = AsyncMock()
    fake_s3.head_object = AsyncMock(
        return_value={
            "ContentLength": 5000,
            "ContentType": "audio/m4a",
            "ETag": '"x"',
        }
    )
    resource, _table = _ddb_resource_mock()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            with patch.object(
                svc, "_get_dynamodb_resource", new=AsyncMock(return_value=resource)
            ):
                result = await svc.finalize_upload(
                    echo_id="echo-1",
                    user_id="u-1",
                    key="echoes/u-1/echo-1_2026.m4a",
                )
    assert result.echo_type == EchoType.AUDIO


@pytest.mark.asyncio
async def test_finalize_upload_maps_403_to_internal_error_without_leaking():
    """403 / AccessDenied is logged as IAM/KMS gap but surfaces a generic error.

    Critical that the error message does NOT contain the HTTP status code or
    the word 'AccessDenied' — that would confirm to a caller that the object
    exists and is just not readable.
    """
    svc = EchoService()
    echo = _make_echo()
    fake_s3 = AsyncMock()
    fake_s3.head_object = AsyncMock(
        side_effect=ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "HeadObject"
        )
    )
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            with pytest.raises(Exception) as exc_info:
                await svc.finalize_upload(
                    echo_id="echo-1",
                    user_id="u-1",
                    key="echoes/u-1/echo-1_2026.mp4",
                )
    msg = str(exc_info.value)
    assert "AccessDenied" not in msg
    assert "403" not in msg
    assert "verify" in msg.lower()


# ----------------------------------------------------------------------
# audio MIME → file extension regression test
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_upload_url_uses_mp3_extension_for_audio_mpeg():
    """audio/mpeg must map to .mp3, not .m4a — fixed in PR1."""
    svc = EchoService()
    fake_s3 = AsyncMock()
    fake_s3.generate_presigned_url = AsyncMock(return_value="signed")
    with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
        result = await svc.generate_upload_url(
            user_id="u-1",
            file_type="audio/mpeg",
            echo_id="e-1",
        )
    assert result["key"].endswith(".mp3"), result["key"]


@pytest.mark.asyncio
async def test_generate_upload_url_uses_aac_extension_for_audio_aac():
    svc = EchoService()
    fake_s3 = AsyncMock()
    fake_s3.generate_presigned_url = AsyncMock(return_value="signed")
    with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
        result = await svc.generate_upload_url(
            user_id="u-1",
            file_type="audio/aac",
            echo_id="e-1",
        )
    assert result["key"].endswith(".aac"), result["key"]


# ----------------------------------------------------------------------
# upload_type service-level allowlist
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_upload_url_rejects_unknown_upload_type():
    """Service-level guard against tag-injection via upload_type."""
    svc = EchoService()
    with pytest.raises(ValidationError, match="Unsupported upload_type"):
        await svc.generate_upload_url(
            user_id="u-1",
            file_type="image/jpeg",
            upload_type="echo&evil=true",
        )
