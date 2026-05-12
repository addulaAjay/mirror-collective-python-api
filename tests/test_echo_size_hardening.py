"""
Tests for the S3-authoritative `size_bytes` hardening in
`EchoService.update_echo`.

When a client patches an echo with `media_url`, the backend must NOT
trust the client-supplied `size_bytes`. It HeadObjects the URL and uses
the actual S3 `ContentLength`. This prevents a tampered client from
declaring `size_bytes: 1` on a 50 GB upload to under-bill the storage
quota.

The hardening matrix:

  media_url in patch | media_url value | HeadObject result | persisted size
  ------------------ | --------------- | ----------------- | --------------
  yes                | s3 url          | ok                | S3 value
  yes                | s3 url          | failure           | client value or unchanged
  yes                | None / empty    | (not called)      | None (cleared)
  yes                | non-s3 url      | (returns None)    | client value or unchanged
  no                 | -               | (not called)      | client value (trusted)
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.models.echo import Echo, EchoStatus, EchoType
from src.app.services.echo_service import EchoService

GB = 1024**3
S3_URL = "https://my-bucket.s3.us-east-1.amazonaws.com/echoes/u1/abc.mp4"


def _mock_dynamo_resource():
    """Stub out the DynamoDB resource so update_echo can put_item without AWS."""
    mock_table = AsyncMock()
    mock_table.put_item = AsyncMock()
    mock_dynamodb = AsyncMock()
    mock_dynamodb.Table = AsyncMock(return_value=mock_table)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_dynamodb)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx, mock_table


def _draft_echo() -> Echo:
    return Echo(
        echo_id="echo-1",
        user_id="user-1",
        title="t",
        echo_type=EchoType.AUDIO,
        status=EchoStatus.DRAFT,
    )


@pytest.mark.asyncio
async def test_s3_size_overrides_client_value_when_media_url_attached():
    """Client claims 1 byte; S3 actually has 1 GB. Persisted value must be 1 GB."""
    service = EchoService()
    ctx, _ = _mock_dynamo_resource()
    head_mock = AsyncMock(return_value=GB)

    with (
        patch.object(service, "get_echo", new=AsyncMock(return_value=_draft_echo())),
        patch.object(service, "_head_object_size", new=head_mock),
        patch.object(service.session, "resource", return_value=ctx),
    ):
        updated = await service.update_echo(
            "echo-1",
            "user-1",
            {"media_url": S3_URL, "size_bytes": 1},
        )

        assert updated.media_url == S3_URL
        assert updated.size_bytes == GB  # NOT the client's 1
        head_mock.assert_awaited_once_with(S3_URL)


@pytest.mark.asyncio
async def test_head_object_failure_falls_back_to_client_size():
    """If HeadObject returns None (transient S3 issue, non-S3 URL), use the
    client-declared size rather than dropping the info entirely.
    """
    service = EchoService()
    ctx, _ = _mock_dynamo_resource()

    with (
        patch.object(service, "get_echo", new=AsyncMock(return_value=_draft_echo())),
        patch.object(service, "_head_object_size", new=AsyncMock(return_value=None)),
        patch.object(service.session, "resource", return_value=ctx),
    ):
        updated = await service.update_echo(
            "echo-1",
            "user-1",
            {"media_url": S3_URL, "size_bytes": 500},
        )

    assert updated.size_bytes == 500


@pytest.mark.asyncio
async def test_head_object_failure_with_no_client_size_leaves_size_unset():
    """No client value AND HeadObject failure → size_bytes stays None.
    The quota service's lazy backfill will retry the HeadObject later.
    """
    service = EchoService()
    ctx, _ = _mock_dynamo_resource()

    with (
        patch.object(service, "get_echo", new=AsyncMock(return_value=_draft_echo())),
        patch.object(service, "_head_object_size", new=AsyncMock(return_value=None)),
        patch.object(service.session, "resource", return_value=ctx),
    ):
        updated = await service.update_echo(
            "echo-1",
            "user-1",
            {"media_url": S3_URL},
        )

    assert updated.size_bytes is None


@pytest.mark.asyncio
async def test_clearing_media_url_clears_size_bytes():
    """An explicit media_url=None patch should also drop the stale size."""
    service = EchoService()
    ctx, _ = _mock_dynamo_resource()

    existing = _draft_echo()
    existing.media_url = S3_URL
    existing.size_bytes = GB

    head_mock = AsyncMock(return_value=None)
    with (
        patch.object(service, "get_echo", new=AsyncMock(return_value=existing)),
        patch.object(service, "_head_object_size", new=head_mock),
        patch.object(service.session, "resource", return_value=ctx),
    ):
        # Allow clearing media_url on a DRAFT echo by widening the
        # is_media_only_update guard; here we just set media_url=None.
        updated = await service.update_echo("echo-1", "user-1", {"media_url": None})

        assert updated.media_url is None
        assert updated.size_bytes is None
        # HeadObject must NOT be called when media is being cleared.
        head_mock.assert_not_called()


@pytest.mark.asyncio
async def test_size_bytes_without_media_url_change_is_trusted():
    """If the patch only sets size_bytes (no media_url touched), keep the
    client value — there's no S3 truth to compare against. Edge case for
    backfill scripts; not the normal upload path.
    """
    service = EchoService()
    ctx, _ = _mock_dynamo_resource()

    existing = _draft_echo()
    existing.media_url = S3_URL  # already attached

    head_mock = AsyncMock(return_value=GB)
    with (
        patch.object(service, "get_echo", new=AsyncMock(return_value=existing)),
        patch.object(service, "_head_object_size", new=head_mock),
        patch.object(service.session, "resource", return_value=ctx),
    ):
        updated = await service.update_echo("echo-1", "user-1", {"size_bytes": 12345})

        assert updated.size_bytes == 12345
        head_mock.assert_not_called()


@pytest.mark.asyncio
async def test_non_s3_media_url_skips_head_object_and_uses_client():
    """Defensive: a non-S3 https URL (e.g., a CDN) won't HeadObject —
    fall back to client value. This is also what the helper returns
    for non-amazonaws URLs in `_head_object_size`.
    """
    service = EchoService()
    ctx, _ = _mock_dynamo_resource()
    non_s3 = "https://cdn.example.com/echoes/abc.mp4"

    with (
        patch.object(service, "get_echo", new=AsyncMock(return_value=_draft_echo())),
        patch.object(service.session, "resource", return_value=ctx),
    ):
        updated = await service.update_echo(
            "echo-1",
            "user-1",
            {"media_url": non_s3, "size_bytes": 42},
        )

    # The real helper returns None for non-amazonaws URLs, so the client
    # value sticks.
    assert updated.media_url == non_s3
    assert updated.size_bytes == 42


@pytest.mark.asyncio
async def test_head_object_helper_returns_none_for_non_amazonaws_url():
    service = EchoService()
    result = await service._head_object_size("https://cdn.example.com/x.mp4")
    assert result is None


@pytest.mark.asyncio
async def test_head_object_helper_returns_none_for_empty_url():
    service = EchoService()
    assert await service._head_object_size(None) is None
    assert await service._head_object_size("") is None


@pytest.mark.asyncio
async def test_head_object_helper_swallows_s3_errors():
    """A real ClientError from S3 must not bubble — we degrade to the
    client-supplied size rather than 500 the whole update.
    """
    service = EchoService()

    mock_s3 = AsyncMock()
    mock_s3.head_object = AsyncMock(side_effect=RuntimeError("S3 down"))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_s3)
    ctx.__aexit__ = AsyncMock(return_value=None)

    with patch.object(service.session, "client", return_value=ctx):
        result = await service._head_object_size(S3_URL)

    assert result is None
