"""Unit tests for EchoService.update_recipient_picture (edit recipient photo).

Picture-only update: name/email are never touched, incoming URLs are
canonicalized + namespace-checked, and the replaced S3 object is cleaned up.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.core.exceptions import ValidationError
from src.app.models.echo import Recipient
from src.app.services.echo_service import EchoService

pytestmark = pytest.mark.asyncio

BUCKET = "echo-vault-media"
REGION = "us-east-1"


def _service() -> EchoService:
    service = EchoService()
    service.s3_bucket = BUCKET
    service.region = REGION
    return service


def _recipient(user_id="u-1", rid="r-1", img=None) -> Recipient:
    return Recipient(
        recipient_id=rid,
        user_id=user_id,
        name="James",
        email="james@email.com",
        profile_image_url=img,
    )


def _canonical(user_id: str, name: str) -> str:
    return f"https://{BUCKET}.s3.{REGION}.amazonaws.com/profiles/{user_id}/{name}"


def _install_ddb(service: EchoService):
    """Mock the put_item path; returns (table_mock, patcher)."""
    table = AsyncMock()
    table.put_item = AsyncMock(return_value=None)
    dynamodb = AsyncMock()
    dynamodb.Table = AsyncMock(return_value=table)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=dynamodb)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return table, patch.object(service.session, "resource", return_value=ctx)


def _install_s3(service: EchoService):
    s3 = AsyncMock()
    s3.generate_presigned_url = AsyncMock(return_value="https://presigned.example/get")
    s3.delete_object = AsyncMock(return_value=None)
    return s3, patch.object(service, "_get_s3_client", AsyncMock(return_value=s3))


async def test_updates_picture_returns_presigned_and_deletes_old():
    service = _service()
    new_url = _canonical("u-1", "new.jpg")
    old_url = _canonical("u-1", "old.jpg")
    existing = _recipient(img=old_url)
    table, ddb = _install_ddb(service)
    s3, s3p = _install_s3(service)

    with (
        patch.object(service, "get_recipient", AsyncMock(return_value=existing)),
        ddb,
        s3p,
    ):
        result = await service.update_recipient_picture("r-1", "u-1", new_url)

    assert result is not None
    persisted = table.put_item.await_args.kwargs["Item"]
    # Canonical URL persisted (not the presigned one).
    assert persisted["profile_image_url"] == new_url
    # name/email are untouched.
    assert persisted["name"] == "James"
    assert persisted["email"] == "james@email.com"
    # Response carries a fresh presigned URL for immediate display.
    assert result.profile_image_url == "https://presigned.example/get"
    # Old image cleaned up.
    s3.delete_object.assert_awaited_once()
    assert s3.delete_object.await_args.kwargs["Key"] == "profiles/u-1/old.jpg"


async def test_returns_none_when_not_owned_or_missing():
    service = _service()
    with patch.object(service, "get_recipient", AsyncMock(return_value=None)):
        result = await service.update_recipient_picture(
            "r-1", "u-1", _canonical("u-1", "x.jpg")
        )
    assert result is None


async def test_strips_presign_query_to_canonical_and_no_delete_when_no_old():
    service = _service()
    existing = _recipient(img=None)
    presigned_input = (
        _canonical("u-1", "new.jpg") + "?X-Amz-Signature=abc&X-Amz-Expires=60"
    )
    table, ddb = _install_ddb(service)
    s3, s3p = _install_s3(service)

    with (
        patch.object(service, "get_recipient", AsyncMock(return_value=existing)),
        ddb,
        s3p,
    ):
        await service.update_recipient_picture("r-1", "u-1", presigned_input)

    persisted = table.put_item.await_args.kwargs["Item"]
    assert persisted["profile_image_url"] == _canonical("u-1", "new.jpg")
    s3.delete_object.assert_not_called()  # no prior image to clean up


async def test_rejects_url_outside_user_namespace():
    service = _service()
    existing = _recipient(user_id="u-1")
    other_users_url = _canonical("u-OTHER", "x.jpg")
    with patch.object(service, "get_recipient", AsyncMock(return_value=existing)):
        with pytest.raises(ValidationError):
            await service.update_recipient_picture("r-1", "u-1", other_users_url)


async def test_rejects_non_s3_url():
    service = _service()
    existing = _recipient(user_id="u-1")
    with patch.object(service, "get_recipient", AsyncMock(return_value=existing)):
        with pytest.raises(ValidationError):
            await service.update_recipient_picture(
                "r-1", "u-1", "https://evil.example/x.jpg"
            )
