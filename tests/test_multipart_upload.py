"""Tests for the S3 multipart upload backend.

Covers each of the four service methods:
  - initiate_multipart_upload: happy path, MIME allowlist, ownership,
    already-finalized-echo rejection, S3 failure.
  - generate_multipart_part_urls: batch-size guard, range guard,
    tenancy reject, parallel fan-out shape.
  - complete_multipart_upload: parts validation, etag canonicalization,
    sort-by-part-number, NoSuchUpload mapped to 404, delegation to
    finalize_upload.
  - abort_multipart_upload: ownership, NoSuchUpload swallowed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.app.core.exceptions import NotFoundError, ValidationError
from src.app.models.echo import Echo, EchoStatus, EchoType
from src.app.services.echo_service import EchoService


def _make_echo(echo_id: str = "echo-1", user_id: str = "u-1", media_url=None) -> Echo:
    return Echo(
        echo_id=echo_id,
        user_id=user_id,
        title="t",
        status=EchoStatus.DRAFT,
        echo_type=EchoType.TEXT,
        media_url=media_url,
        recipient_id="r-1",
    )


# ----------------------------------------------------------------------
# initiate_multipart_upload
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initiate_rejects_unknown_mime():
    svc = EchoService()
    echo = _make_echo()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(ValidationError, match="Unsupported media type"):
            await svc.initiate_multipart_upload(
                echo_id="echo-1",
                user_id="u-1",
                file_type="application/x-bad",
            )


@pytest.mark.asyncio
async def test_initiate_rejects_when_echo_missing():
    svc = EchoService()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=None)):
        with pytest.raises(NotFoundError):
            await svc.initiate_multipart_upload(
                echo_id="echo-1",
                user_id="u-1",
                file_type="video/mp4",
            )


@pytest.mark.asyncio
async def test_initiate_rejects_recipient_caller_as_notfound():
    """Owner-only — recipients see NotFound to avoid info leakage."""
    svc = EchoService()
    echo = _make_echo(user_id="owner-u")
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(NotFoundError):
            await svc.initiate_multipart_upload(
                echo_id="echo-1",
                user_id="recipient-u",
                file_type="video/mp4",
            )


@pytest.mark.asyncio
async def test_initiate_rejects_when_media_already_attached():
    svc = EchoService()
    echo = _make_echo(media_url="https://b/.../already.mp4")
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(ValidationError, match="already been finalized"):
            await svc.initiate_multipart_upload(
                echo_id="echo-1",
                user_id="u-1",
                file_type="video/mp4",
            )


@pytest.mark.asyncio
async def test_initiate_returns_upload_id_and_key_with_owner_prefix():
    svc = EchoService()
    svc.s3_bucket = "mc-bucket"
    echo = _make_echo()
    fake_s3 = AsyncMock()
    fake_s3.create_multipart_upload = AsyncMock(
        return_value={"UploadId": "UPLOAD-ABC123"}
    )
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            result = await svc.initiate_multipart_upload(
                echo_id="echo-1",
                user_id="u-1",
                file_type="video/mp4",
            )

    assert result["upload_id"] == "UPLOAD-ABC123"
    assert result["bucket"] == "mc-bucket"
    assert result["key"].startswith("echoes/u-1/echo-1_")
    assert result["key"].endswith(".mp4")

    # Verify the create_multipart_upload call carried cache-control + tags + metadata.
    kwargs = fake_s3.create_multipart_upload.call_args.kwargs
    assert kwargs["ContentType"] == "video/mp4"
    assert kwargs["CacheControl"] == "public, max-age=31536000, immutable"
    assert "user_id=u-1" in kwargs["Tagging"]
    assert "echo_id=echo-1" in kwargs["Tagging"]
    assert kwargs["Metadata"]["echo_id"] == "echo-1"


@pytest.mark.asyncio
async def test_initiate_maps_s3_failure_to_internal_error():
    from src.app.core.exceptions import InternalServerError

    svc = EchoService()
    echo = _make_echo()
    fake_s3 = AsyncMock()
    fake_s3.create_multipart_upload = AsyncMock(
        side_effect=ClientError(
            {"Error": {"Code": "InternalError"}}, "CreateMultipartUpload"
        )
    )
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            with pytest.raises(InternalServerError):
                await svc.initiate_multipart_upload(
                    echo_id="echo-1",
                    user_id="u-1",
                    file_type="video/mp4",
                )


# ----------------------------------------------------------------------
# generate_multipart_part_urls
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_part_urls_rejects_empty_batch():
    svc = EchoService()
    echo = _make_echo()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(ValidationError, match="cannot be empty"):
            await svc.generate_multipart_part_urls(
                echo_id="echo-1",
                user_id="u-1",
                upload_id="UPLOAD",
                key="echoes/u-1/echo-1_x.mp4",
                part_numbers=[],
            )


@pytest.mark.asyncio
async def test_part_urls_rejects_oversize_batch():
    svc = EchoService()
    echo = _make_echo()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(ValidationError, match="batch exceeds"):
            await svc.generate_multipart_part_urls(
                echo_id="echo-1",
                user_id="u-1",
                upload_id="UPLOAD",
                key="echoes/u-1/echo-1_x.mp4",
                part_numbers=list(range(1, 1002)),  # 1001 parts > 1000 cap
            )


@pytest.mark.parametrize("bad", [0, -1, 10_001, 10_002])
@pytest.mark.asyncio
async def test_part_urls_rejects_out_of_range_part_number(bad):
    svc = EchoService()
    with pytest.raises(ValidationError, match="out of range"):
        await svc.generate_multipart_part_urls(
            echo_id="echo-1",
            user_id="u-1",
            upload_id="UPLOAD",
            key="echoes/u-1/echo-1_x.mp4",
            part_numbers=[1, 2, bad],
        )


@pytest.mark.asyncio
async def test_part_urls_rejects_cross_tenant_key():
    svc = EchoService()
    echo = _make_echo()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(ValidationError, match="does not belong to this echo"):
            await svc.generate_multipart_part_urls(
                echo_id="echo-1",
                user_id="u-1",
                upload_id="UPLOAD",
                key="echoes/other-user/echo-1_x.mp4",
                part_numbers=[1],
            )


@pytest.mark.asyncio
async def test_part_urls_rejects_recipient_caller_as_notfound():
    svc = EchoService()
    echo = _make_echo(user_id="owner-u")
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(NotFoundError):
            await svc.generate_multipart_part_urls(
                echo_id="echo-1",
                user_id="recipient-u",
                upload_id="UPLOAD",
                key="echoes/recipient-u/echo-1_x.mp4",
                part_numbers=[1],
            )


@pytest.mark.asyncio
async def test_part_urls_returns_one_url_per_requested_part():
    svc = EchoService()
    svc.s3_bucket = "mc-bucket"
    echo = _make_echo()
    fake_s3 = AsyncMock()
    # generate_presigned_url is called once per part with different
    # PartNumber values; return a distinct URL per call.
    call_count = {"n": 0}

    async def fake_sign(*_args, **kwargs):
        call_count["n"] += 1
        return f"signed-{kwargs['Params']['PartNumber']}"

    fake_s3.generate_presigned_url = fake_sign

    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            result = await svc.generate_multipart_part_urls(
                echo_id="echo-1",
                user_id="u-1",
                upload_id="UPLOAD",
                key="echoes/u-1/echo-1_x.mp4",
                part_numbers=[1, 2, 3],
            )

    assert call_count["n"] == 3
    assert result == [
        {"part_number": 1, "url": "signed-1"},
        {"part_number": 2, "url": "signed-2"},
        {"part_number": 3, "url": "signed-3"},
    ]


# ----------------------------------------------------------------------
# complete_multipart_upload
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_rejects_empty_parts():
    svc = EchoService()
    with pytest.raises(ValidationError, match="parts cannot be empty"):
        await svc.complete_multipart_upload(
            echo_id="echo-1",
            user_id="u-1",
            upload_id="UPLOAD",
            key="echoes/u-1/echo-1_x.mp4",
            parts=[],
        )


@pytest.mark.asyncio
async def test_complete_rejects_part_with_missing_etag():
    svc = EchoService()
    with pytest.raises(ValidationError, match="missing etag"):
        await svc.complete_multipart_upload(
            echo_id="echo-1",
            user_id="u-1",
            upload_id="UPLOAD",
            key="echoes/u-1/echo-1_x.mp4",
            parts=[{"part_number": 1, "etag": ""}],
        )


@pytest.mark.asyncio
async def test_complete_canonicalizes_unquoted_etags_and_sorts_by_part_number():
    """ETags from S3's wire format are quoted; clients sometimes strip
    the quotes when reading from response headers. Service must
    accept both forms and always pass quoted to S3 (which is what
    CompleteMultipartUpload expects).
    """
    svc = EchoService()
    echo = _make_echo()
    fake_s3 = AsyncMock()
    fake_s3.complete_multipart_upload = AsyncMock(return_value={})
    finalized = _make_echo(media_url="https://b/.../echo-1.mp4")
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            with patch.object(
                svc, "finalize_upload", new=AsyncMock(return_value=finalized)
            ):
                # Pass parts deliberately out of order, with mixed quoting.
                await svc.complete_multipart_upload(
                    echo_id="echo-1",
                    user_id="u-1",
                    upload_id="UPLOAD",
                    key="echoes/u-1/echo-1_x.mp4",
                    parts=[
                        {"part_number": 3, "etag": "etag-3"},
                        {"part_number": 1, "etag": '"etag-1"'},
                        {"part_number": 2, "etag": "etag-2"},
                    ],
                )

    sent = fake_s3.complete_multipart_upload.call_args.kwargs["MultipartUpload"][
        "Parts"
    ]
    # Sorted ascending by PartNumber.
    assert [p["PartNumber"] for p in sent] == [1, 2, 3]
    # All quoted.
    assert sent[0]["ETag"] == '"etag-1"'
    assert sent[1]["ETag"] == '"etag-2"'
    assert sent[2]["ETag"] == '"etag-3"'


@pytest.mark.asyncio
async def test_complete_maps_NoSuchUpload_to_notfound():
    svc = EchoService()
    echo = _make_echo()
    fake_s3 = AsyncMock()
    fake_s3.complete_multipart_upload = AsyncMock(
        side_effect=ClientError(
            {"Error": {"Code": "NoSuchUpload"}}, "CompleteMultipartUpload"
        )
    )
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            with pytest.raises(NotFoundError, match="session expired"):
                await svc.complete_multipart_upload(
                    echo_id="echo-1",
                    user_id="u-1",
                    upload_id="UPLOAD",
                    key="echoes/u-1/echo-1_x.mp4",
                    parts=[{"part_number": 1, "etag": "x"}],
                )


@pytest.mark.asyncio
async def test_complete_rejects_cross_tenant_key():
    svc = EchoService()
    echo = _make_echo()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(ValidationError, match="does not belong to this echo"):
            await svc.complete_multipart_upload(
                echo_id="echo-1",
                user_id="u-1",
                upload_id="UPLOAD",
                key="echoes/other-user/echo-1_x.mp4",
                parts=[{"part_number": 1, "etag": "x"}],
            )


# ----------------------------------------------------------------------
# abort_multipart_upload
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_abort_calls_s3_with_upload_id():
    svc = EchoService()
    echo = _make_echo()
    fake_s3 = AsyncMock()
    fake_s3.abort_multipart_upload = AsyncMock(return_value={})
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            await svc.abort_multipart_upload(
                echo_id="echo-1",
                user_id="u-1",
                upload_id="UPLOAD",
                key="echoes/u-1/echo-1_x.mp4",
            )
    fake_s3.abort_multipart_upload.assert_awaited_once()
    kwargs = fake_s3.abort_multipart_upload.call_args.kwargs
    assert kwargs["UploadId"] == "UPLOAD"


@pytest.mark.asyncio
async def test_abort_swallows_NoSuchUpload():
    """Already-gone upload is the desired end state."""
    svc = EchoService()
    echo = _make_echo()
    fake_s3 = AsyncMock()
    fake_s3.abort_multipart_upload = AsyncMock(
        side_effect=ClientError(
            {"Error": {"Code": "NoSuchUpload"}}, "AbortMultipartUpload"
        )
    )
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            # Should NOT raise.
            await svc.abort_multipart_upload(
                echo_id="echo-1",
                user_id="u-1",
                upload_id="UPLOAD",
                key="echoes/u-1/echo-1_x.mp4",
            )


@pytest.mark.asyncio
async def test_abort_rejects_recipient_caller():
    svc = EchoService()
    echo = _make_echo(user_id="owner-u")
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(NotFoundError):
            await svc.abort_multipart_upload(
                echo_id="echo-1",
                user_id="recipient-u",
                upload_id="UPLOAD",
                key="echoes/recipient-u/echo-1_x.mp4",
            )
