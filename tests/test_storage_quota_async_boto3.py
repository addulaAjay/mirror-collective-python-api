"""
Tests for the asyncio.to_thread wrapping of S3 paginator iteration in
StorageQuotaService.calculate_user_storage_usage.

The full paginator iteration is now offloaded to a worker thread so a
chatty user with many objects doesn't block the FastAPI event loop for
the duration of pagination.
"""

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def quota_service_with_mock_s3(monkeypatch):
    """Build a StorageQuotaService with a MagicMock S3 client.

    Uses ``monkeypatch.setenv`` instead of ``os.environ.setdefault`` because
    setdefault is a no-op when ECHO_MEDIA_BUCKET is already set by the
    project's ``.env`` or by another test fixture — and StorageQuotaService
    reads the env var at __init__ time, baking the wrong value into
    ``service.bucket``.
    """
    monkeypatch.setenv("ECHO_MEDIA_BUCKET", "test-bucket")
    mock_s3 = MagicMock()
    with patch("boto3.client", return_value=mock_s3):
        from src.app.services.storage_quota_service import StorageQuotaService

        mock_dynamodb = AsyncMock()
        service = StorageQuotaService(mock_dynamodb)
        service.s3_client = mock_s3
        yield service, mock_s3


def test_s3_client_constructed_with_max_pool_connections():
    """boto3.client must receive a Config with max_pool_connections=50."""
    with patch("src.app.services.storage_quota_service.boto3.client") as mock_boto3:
        mock_boto3.return_value = MagicMock()
        from src.app.services.storage_quota_service import StorageQuotaService

        StorageQuotaService(AsyncMock())

    args, kwargs = mock_boto3.call_args
    assert args[0] == "s3"
    assert "config" in kwargs
    config = kwargs["config"]
    assert config.max_pool_connections == 50
    # botocore.Config sets `retries` via __setattr__; not in the type stubs.
    assert config.retries == {"max_attempts": 5, "mode": "adaptive"}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_calculate_user_storage_usage_iterates_in_threadpool(
    quota_service_with_mock_s3,
):
    """calculate_user_storage_usage must drain the paginator via to_thread."""
    service, mock_s3 = quota_service_with_mock_s3

    pages = [
        {"Contents": [{"Size": 1024**3}, {"Size": 2 * 1024**3}]},  # 3 GB
        {"Contents": [{"Size": 5 * (1024**3)}]},  # 5 GB
    ]

    mock_paginator = MagicMock()
    mock_paginator.paginate.return_value = iter(pages)
    mock_s3.get_paginator.return_value = mock_paginator

    result = await service.calculate_user_storage_usage("user-1")

    assert result == 8.0
    mock_s3.get_paginator.assert_called_with("list_objects_v2")
    mock_paginator.paginate.assert_called_with(
        Bucket="test-bucket", Prefix="users/user-1/"
    )


@pytest.mark.asyncio
async def test_calculate_user_storage_usage_returns_zero_on_error(
    quota_service_with_mock_s3,
):
    """Errors during pagination must be swallowed and return 0.0."""
    service, mock_s3 = quota_service_with_mock_s3
    mock_s3.get_paginator.side_effect = RuntimeError("boom")

    result = await service.calculate_user_storage_usage("user-bad")
    assert result == 0.0


@pytest.mark.asyncio
async def test_calculate_user_storage_usage_concurrent_overlaps(
    quota_service_with_mock_s3,
):
    """
    Five concurrent calculate_user_storage_usage calls — each with a slow
    paginator — must overlap on the threadpool.
    """
    service, mock_s3 = quota_service_with_mock_s3
    sleep_ms = 100

    def slow_paginate(**kwargs):
        time.sleep(sleep_ms / 1000.0)
        return iter([{"Contents": [{"Size": 1024**3}]}])  # 1 GB

    mock_paginator = MagicMock()
    mock_paginator.paginate.side_effect = slow_paginate
    mock_s3.get_paginator.return_value = mock_paginator

    start = time.perf_counter()
    results = await asyncio.gather(
        *(service.calculate_user_storage_usage(f"user-{i}") for i in range(5))
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert results == [1.0] * 5
    assert elapsed_ms < sleep_ms * 3, (
        f"Storage quota calls did not overlap: elapsed={elapsed_ms:.1f}ms "
        f"(expected ~{sleep_ms}ms, serialized would be ~{5 * sleep_ms}ms)"
    )
