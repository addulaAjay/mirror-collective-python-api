"""
Tests for the per-echo size aggregation introduced to replace the per-call
S3 inventory scan in `StorageQuotaService.calculate_user_storage_usage`.

Cases covered:
  - all rows already have `size_bytes` → sum without touching S3
  - some rows missing `size_bytes` → backfill from HeadObject, persist
  - rows without `media_url` (e.g., TEXT echoes) are skipped, no S3 call
  - soft-deleted rows still count (matches S3 reality / product intent)
  - HeadObject failure on a single row degrades to "skip that row" rather
    than 500-ing the whole aggregation
  - DynamoDB returns Decimal for numeric attributes — coercion is correct
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

GB = 1024**3


def _make_service(items, head_object=None):
    """Build a StorageQuotaService with a stubbed dynamodb + S3 client."""
    from src.app.services.storage_quota_service import StorageQuotaService

    mock_dynamodb = AsyncMock()
    mock_dynamodb.query_items = AsyncMock(return_value=items)
    mock_dynamodb.update_item = AsyncMock(return_value=True)

    service = StorageQuotaService(mock_dynamodb)

    mock_s3 = MagicMock()
    if head_object is not None:
        mock_s3.head_object = MagicMock(return_value=head_object)
    service.s3_client = mock_s3
    return service, mock_dynamodb, mock_s3


@pytest.mark.asyncio
async def test_sum_when_all_rows_have_size_bytes():
    items = [
        {"echo_id": "e1", "user_id": "u1", "size_bytes": GB},
        {"echo_id": "e2", "user_id": "u1", "size_bytes": 2 * GB},
        # TEXT echo — no media, no size
        {"echo_id": "e3", "user_id": "u1"},
    ]
    service, mock_dynamodb, mock_s3 = _make_service(items)

    used = await service.calculate_user_storage_usage("u1")

    assert used == 3.0  # 3 GB
    mock_dynamodb.query_items.assert_awaited_once()
    mock_s3.head_object.assert_not_called()
    mock_dynamodb.update_item.assert_not_called()


@pytest.mark.asyncio
async def test_decimal_size_bytes_is_coerced_to_int():
    items = [
        # DynamoDB returns numeric attributes as Decimal
        {"echo_id": "e1", "user_id": "u1", "size_bytes": Decimal(str(GB // 2))},
        {"echo_id": "e2", "user_id": "u1", "size_bytes": Decimal(str(GB // 2))},
    ]
    service, _, _ = _make_service(items)

    used = await service.calculate_user_storage_usage("u1")

    assert used == 1.0  # 0.5 + 0.5 = 1 GB


@pytest.mark.asyncio
async def test_lazy_backfill_via_head_object_persists_size():
    items = [
        # Legacy row: media_url present, size_bytes missing
        {
            "echo_id": "legacy-1",
            "user_id": "u1",
            "media_url": (
                "https://my-bucket.s3.us-east-1.amazonaws.com/"
                "echoes/u1/legacy-1_20260101.mp4"
            ),
        },
    ]
    head = {"ContentLength": GB}
    service, mock_dynamodb, mock_s3 = _make_service(items, head_object=head)

    used = await service.calculate_user_storage_usage("u1")

    assert used == 1.0
    mock_s3.head_object.assert_called_once()
    # The backfilled size was persisted to DynamoDB
    mock_dynamodb.update_item.assert_awaited_once()
    update_kwargs = mock_dynamodb.update_item.await_args.kwargs
    assert update_kwargs["key"] == {"echo_id": "legacy-1"}
    assert update_kwargs["expression_values"] == {":s": GB}


@pytest.mark.asyncio
async def test_text_echo_without_media_url_is_skipped():
    items = [
        {"echo_id": "txt-1", "user_id": "u1", "content": "hello"},
    ]
    service, _, mock_s3 = _make_service(items)

    used = await service.calculate_user_storage_usage("u1")

    assert used == 0.0
    mock_s3.head_object.assert_not_called()


@pytest.mark.asyncio
async def test_soft_deleted_rows_still_count():
    """Echo Vault uses soft-delete by product decision (history must be
    preserved). The S3 object stays, so the quota number should reflect it.
    """
    items = [
        {
            "echo_id": "active",
            "user_id": "u1",
            "size_bytes": GB,
        },
        {
            "echo_id": "deleted",
            "user_id": "u1",
            "size_bytes": GB,
            "deleted_at": "2026-05-12T00:00:00Z",
        },
    ]
    service, _, _ = _make_service(items)

    used = await service.calculate_user_storage_usage("u1")

    assert used == 2.0  # both rows counted


@pytest.mark.asyncio
async def test_head_object_failure_does_not_500():
    items = [
        {
            "echo_id": "legacy-broken",
            "user_id": "u1",
            "media_url": (
                "https://my-bucket.s3.us-east-1.amazonaws.com/"
                "echoes/u1/legacy-broken.mp4"
            ),
        },
        {"echo_id": "ok", "user_id": "u1", "size_bytes": GB},
    ]
    service, mock_dynamodb, mock_s3 = _make_service(items)
    mock_s3.head_object = MagicMock(side_effect=RuntimeError("S3 down"))

    used = await service.calculate_user_storage_usage("u1")

    # Broken row contributes 0; healthy row still counted
    assert used == 1.0
    mock_dynamodb.update_item.assert_not_called()


@pytest.mark.asyncio
async def test_dynamo_error_returns_zero_and_does_not_raise():
    from src.app.services.storage_quota_service import StorageQuotaService

    mock_dynamodb = AsyncMock()
    mock_dynamodb.query_items = AsyncMock(side_effect=RuntimeError("boom"))
    service = StorageQuotaService(mock_dynamodb)

    used = await service.calculate_user_storage_usage("u1")
    assert used == 0.0


@pytest.mark.asyncio
async def test_non_integer_size_bytes_treated_as_missing():
    """Defensive: if a stray non-int slips into the column, fall back to S3
    backfill rather than crashing the whole aggregation."""
    items = [
        {
            "echo_id": "weird",
            "user_id": "u1",
            "size_bytes": "not-a-number",
            "media_url": "https://b.s3.r.amazonaws.com/echoes/u1/weird.mp4",
        },
    ]
    head = {"ContentLength": 2 * GB}
    service, _, mock_s3 = _make_service(items, head_object=head)

    used = await service.calculate_user_storage_usage("u1")

    assert used == 2.0
    mock_s3.head_object.assert_called_once()
