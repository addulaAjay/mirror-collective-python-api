"""
Tests for the SNS service async variants and max_pool_connections config.

SNS sync methods are still called from a background APScheduler thread
(services/scheduler.py), so the sync surface is preserved. The new
*_async variants delegate to the sync methods via asyncio.to_thread,
giving FastAPI routes a non-blocking option.
"""

import asyncio
import os
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def sns_service_with_mock_client():
    """Build an SNSService whose boto3 client is a MagicMock."""
    os.environ.setdefault("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:test")
    os.environ.setdefault("AWS_SNS_REGION", "us-east-1")
    os.environ.setdefault("SNS_ANDROID_APP_ARN", "arn:aws:sns:us-east-1:123:android")
    os.environ.setdefault("SNS_IOS_APP_ARN", "arn:aws:sns:us-east-1:123:ios")

    mock_client = MagicMock()
    with patch("boto3.client", return_value=mock_client):
        from src.app.services.sns_service import SNSService

        service = SNSService()
        service._sns = mock_client  # inject the mock as the lazily-built client
        yield service, mock_client


def test_client_constructed_with_max_pool_connections():
    """boto3.client must receive a Config with max_pool_connections=50."""
    os.environ.setdefault("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:123:test")

    with patch("src.app.services.sns_service.boto3.client") as mock_boto3:
        mock_boto3.return_value = MagicMock()
        from src.app.services.sns_service import SNSService

        # The client is built lazily; access .sns to trigger construction.
        _ = SNSService().sns

    args, kwargs = mock_boto3.call_args
    assert args[0] == "sns"
    assert "config" in kwargs
    config = kwargs["config"]
    assert config.max_pool_connections == 50
    # botocore.Config sets `retries` via __setattr__; not in the type stubs.
    assert config.retries == {"max_attempts": 5, "mode": "adaptive"}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_publish_to_topic_async_delegates(sns_service_with_mock_client):
    """publish_to_topic_async must call publish on the underlying client."""
    service, mock_client = sns_service_with_mock_client
    mock_client.publish.return_value = {"MessageId": "msg-1"}

    msg_id = await service.publish_to_topic_async("Title", "Body", {"k": "v"})

    assert msg_id == "msg-1"
    mock_client.publish.assert_called_once()
    kwargs = mock_client.publish.call_args.kwargs
    assert kwargs["TopicArn"] == service.topic_arn
    assert kwargs["MessageStructure"] == "json"


@pytest.mark.asyncio
async def test_create_platform_endpoint_async_delegates(sns_service_with_mock_client):
    service, mock_client = sns_service_with_mock_client
    mock_client.create_platform_endpoint.return_value = {"EndpointArn": "endpoint-1"}

    result = await service.create_platform_endpoint_async(
        token="device-token", platform="android", user_id="user-42"
    )

    assert result == "endpoint-1"
    kwargs = mock_client.create_platform_endpoint.call_args.kwargs
    assert kwargs["Token"] == "device-token"
    assert kwargs["CustomUserData"] == "user-42"


@pytest.mark.asyncio
async def test_publish_to_endpoint_async_overlaps_concurrently(
    sns_service_with_mock_client,
):
    """5 concurrent async publishes must overlap (run on threadpool)."""
    service, mock_client = sns_service_with_mock_client
    sleep_ms = 100

    # The previous assertion `results == [f"endpoint-{i}" ...]` only passed
    # by coincidence: the mock returned {"MessageId": kwargs["TargetArn"]}
    # and the TargetArn equaled the expected string. Make the mock value
    # explicit so the assertion is testing what we mean (overlap), not the
    # mock's coincidental return shape.
    expected_msg_id = "test-msg-id"

    def slow_publish(**kwargs):
        time.sleep(sleep_ms / 1000.0)
        return {"MessageId": expected_msg_id}

    mock_client.publish.side_effect = slow_publish

    start = time.perf_counter()
    results = await asyncio.gather(
        *(
            service.publish_to_endpoint_async(f"endpoint-{i}", "T", "B")
            for i in range(5)
        )
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    # All 5 calls succeed and return the mock's MessageId.
    assert len(results) == 5
    assert all(r == expected_msg_id for r in results)
    # And — the whole point of this test — they overlapped on the threadpool.
    assert elapsed_ms < sleep_ms * 3, (
        f"SNS async calls did not overlap: elapsed={elapsed_ms:.1f}ms "
        f"(expected ~{sleep_ms}ms, serialized would be ~{5 * sleep_ms}ms)"
    )


def test_sync_publish_to_topic_still_works(sns_service_with_mock_client):
    """The original sync method must remain available for the scheduler."""
    service, mock_client = sns_service_with_mock_client
    mock_client.publish.return_value = {"MessageId": "msg-sync"}

    msg_id = service.publish_to_topic("Title", "Body")

    assert msg_id == "msg-sync"
    mock_client.publish.assert_called_once()


def test_sns_client_is_lazy():
    """The boto3 SNS client must not be built until first use (cold-start perf)."""
    from unittest.mock import patch

    from src.app.services.sns_service import SNSService

    with patch("src.app.services.sns_service.boto3.client") as mock_client:
        svc = SNSService()
        assert mock_client.call_count == 0  # not built at construction
        _ = svc.sns  # first access builds it
        assert mock_client.call_count == 1
        _ = svc.sns  # cached
        assert mock_client.call_count == 1
