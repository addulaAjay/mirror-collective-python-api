"""Tests for the video-poster attach + signing.

Covers:
- EchoService.attach_poster: owner-only enforcement, tenancy on key
  prefix, must-have-media precondition, HeadObject success / missing /
  access-denied paths, DDB write.
- _sign_media_url now also signs poster_url when present.
- _sign_poster_urls_for_echoes parallel signer for list endpoints.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.app.core.exceptions import InternalServerError, NotFoundError, ValidationError
from src.app.models.echo import Echo, EchoStatus, EchoType
from src.app.services.echo_service import EchoService


def _make_echo(
    echo_id: str = "echo-1",
    user_id: str = "u-1",
    media_url: (
        str | None
    ) = "https://b.s3.us-east-1.amazonaws.com/echoes/u-1/echo-1_x.mp4",
    poster_url: str | None = None,
) -> Echo:
    return Echo(
        echo_id=echo_id,
        user_id=user_id,
        title="t",
        status=EchoStatus.RELEASED,
        echo_type=EchoType.VIDEO,
        media_url=media_url,
        poster_url=poster_url,
        recipient_id="r-1",
    )


def _ddb_resource_mock() -> tuple[AsyncMock, AsyncMock]:
    table = AsyncMock()
    table.put_item = AsyncMock()
    resource = AsyncMock()
    resource.Table = AsyncMock(return_value=table)
    return resource, table


# ---------------------------------------------------------------------
# attach_poster — happy path + guards
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attach_poster_writes_canonical_url_after_HEAD():
    svc = EchoService()
    svc.s3_bucket = "mc-bucket"
    svc.region = "us-east-1"

    echo = _make_echo()
    fake_s3 = AsyncMock()
    fake_s3.head_object = AsyncMock(
        return_value={"ContentLength": 1024, "ContentType": "image/jpeg"}
    )
    resource, table = _ddb_resource_mock()

    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            with patch.object(
                svc, "_get_dynamodb_resource", new=AsyncMock(return_value=resource)
            ):
                result = await svc.attach_poster(
                    echo_id="echo-1",
                    user_id="u-1",
                    key="echoes/u-1/echo-1_poster.jpg",
                )

    assert result.poster_url == (
        "https://mc-bucket.s3.us-east-1.amazonaws.com/echoes/u-1/echo-1_poster.jpg"
    )
    table.put_item.assert_awaited_once()


@pytest.mark.asyncio
async def test_attach_poster_rejects_recipient_caller_as_notfound():
    """Recipients get NotFound to avoid info leakage on echo existence."""
    svc = EchoService()
    echo = _make_echo(user_id="owner-u")
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(NotFoundError):
            await svc.attach_poster(
                echo_id="echo-1",
                user_id="recipient-u",
                key="echoes/recipient-u/echo-1_poster.jpg",
            )


@pytest.mark.asyncio
async def test_attach_poster_rejects_when_no_media():
    """Poster attaches to existing media; calling early would orphan
    the poster on a media-less echo row.
    """
    svc = EchoService()
    echo = _make_echo(media_url=None)
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(ValidationError, match="no media"):
            await svc.attach_poster(
                echo_id="echo-1",
                user_id="u-1",
                key="echoes/u-1/echo-1_poster.jpg",
            )


@pytest.mark.asyncio
async def test_attach_poster_rejects_cross_tenant_key():
    svc = EchoService()
    echo = _make_echo()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with pytest.raises(ValidationError, match="does not belong"):
            await svc.attach_poster(
                echo_id="echo-1",
                user_id="u-1",
                key="echoes/other-user/echo-1_poster.jpg",
            )


@pytest.mark.asyncio
async def test_attach_poster_rejects_when_object_missing():
    svc = EchoService()
    echo = _make_echo()
    fake_s3 = AsyncMock()
    fake_s3.head_object = AsyncMock(
        side_effect=ClientError({"Error": {"Code": "404"}}, "HeadObject")
    )
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            with pytest.raises(NotFoundError, match="No uploaded poster"):
                await svc.attach_poster(
                    echo_id="echo-1",
                    user_id="u-1",
                    key="echoes/u-1/echo-1_poster.jpg",
                )


@pytest.mark.asyncio
async def test_attach_poster_maps_403_to_internal_without_leaking():
    """Access-denied must NOT confirm the object exists."""
    svc = EchoService()
    echo = _make_echo()
    fake_s3 = AsyncMock()
    fake_s3.head_object = AsyncMock(
        side_effect=ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")
    )
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            with pytest.raises(InternalServerError) as exc_info:
                await svc.attach_poster(
                    echo_id="echo-1",
                    user_id="u-1",
                    key="echoes/u-1/echo-1_poster.jpg",
                )
    msg = str(exc_info.value)
    assert "AccessDenied" not in msg
    assert "403" not in msg


@pytest.mark.asyncio
async def test_attach_poster_allows_overwrite():
    """Unlike media_url, poster_url isn't first-write — a retry that
    overwrites the same URL is a no-op user-visibly.
    """
    svc = EchoService()
    echo = _make_echo(poster_url="https://b.s3.amazonaws.com/old-poster.jpg")
    fake_s3 = AsyncMock()
    fake_s3.head_object = AsyncMock(
        return_value={"ContentLength": 1024, "ContentType": "image/jpeg"}
    )
    resource, _table = _ddb_resource_mock()
    with patch.object(svc, "get_echo", new=AsyncMock(return_value=echo)):
        with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
            with patch.object(
                svc, "_get_dynamodb_resource", new=AsyncMock(return_value=resource)
            ):
                # Should NOT raise — overwrite allowed.
                result = await svc.attach_poster(
                    echo_id="echo-1",
                    user_id="u-1",
                    key="echoes/u-1/echo-1_poster.jpg",
                )
    assert "echo-1_poster.jpg" in (result.poster_url or "")


# ---------------------------------------------------------------------
# _sign_media_url signs poster_url alongside media_url
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sign_media_url_also_signs_poster_url():
    svc = EchoService()
    echo = _make_echo(
        media_url="https://b.s3.us-east-1.amazonaws.com/echoes/u/e_x.mp4",
        poster_url="https://b.s3.us-east-1.amazonaws.com/echoes/u/e_x.jpg",
    )
    fake_s3 = AsyncMock()
    # Different signed URLs for each call so we can verify both ran.
    call_returns = iter(
        [
            "https://signed.example/media?sig=1",
            "https://signed.example/poster?sig=2",
        ]
    )
    fake_s3.generate_presigned_url = AsyncMock(
        side_effect=lambda *_, **__: next(call_returns)
    )
    with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
        signed = await svc._sign_media_url(echo)

    assert signed.media_url == "https://signed.example/media?sig=1"
    assert signed.poster_url == "https://signed.example/poster?sig=2"


@pytest.mark.asyncio
async def test_sign_media_url_handles_missing_poster_gracefully():
    """An echo without poster_url should sign media_url only, no error."""
    svc = EchoService()
    echo = _make_echo(poster_url=None)
    fake_s3 = AsyncMock()
    fake_s3.generate_presigned_url = AsyncMock(return_value="signed-media")
    with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
        signed = await svc._sign_media_url(echo)
    assert signed.media_url == "signed-media"
    assert signed.poster_url is None
    # Only one call — media, not poster.
    assert fake_s3.generate_presigned_url.await_count == 1


# ---------------------------------------------------------------------
# _sign_poster_urls_for_echoes — list-endpoint parallel signer
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sign_poster_urls_for_echoes_signs_in_parallel():
    """Three echoes with posters → exactly 3 sign calls, all in parallel.
    Echoes without posters are skipped (no sign call).
    """
    svc = EchoService()
    echoes = [
        _make_echo(
            echo_id=f"e-{i}",
            poster_url=f"https://b.s3.us-east-1.amazonaws.com/echoes/u/e-{i}.jpg",
        )
        for i in range(3)
    ] + [_make_echo(echo_id="e-no-poster", poster_url=None)]

    sign_calls: list[str] = []
    fake_s3 = AsyncMock()

    async def fake_sign(*_, Params, **__):
        sign_calls.append(Params["Key"])
        return f"signed::{Params['Key']}"

    fake_s3.generate_presigned_url = fake_sign

    with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
        await svc._sign_poster_urls_for_echoes(echoes)

    # Three signs (e-0, e-1, e-2) — e-no-poster was skipped.
    assert len(sign_calls) == 3
    # All three echoes with posters got their URLs signed.
    for i in range(3):
        assert echoes[i].poster_url == f"signed::echoes/u/e-{i}.jpg"
    # No poster on the fourth echo — unchanged.
    assert echoes[3].poster_url is None


@pytest.mark.asyncio
async def test_sign_poster_urls_for_echoes_handles_empty_list():
    svc = EchoService()
    await svc._sign_poster_urls_for_echoes([])  # should not raise


@pytest.mark.asyncio
async def test_sign_poster_urls_for_echoes_keeps_original_on_failure():
    """A sign failure for one echo doesn't affect the others."""
    svc = EchoService()
    echoes = [
        _make_echo(
            echo_id=f"e-{i}",
            poster_url=f"https://b.s3.us-east-1.amazonaws.com/echoes/u/e-{i}.jpg",
        )
        for i in range(2)
    ]
    originals = [e.poster_url for e in echoes]

    fake_s3 = AsyncMock()

    async def fake_sign(*_, Params, **__):
        if "e-0" in Params["Key"]:
            raise ClientError({"Error": {"Code": "X"}}, "GetObject")
        return f"signed::{Params['Key']}"

    fake_s3.generate_presigned_url = fake_sign

    with patch.object(svc, "_get_s3_client", new=AsyncMock(return_value=fake_s3)):
        await svc._sign_poster_urls_for_echoes(echoes)

    # e-0 keeps its original URL (sign failed); e-1 is signed.
    assert echoes[0].poster_url == originals[0]
    assert echoes[1].poster_url == "signed::echoes/u/e-1.jpg"
