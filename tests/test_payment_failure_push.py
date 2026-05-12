"""
Tests for `SubscriptionService._send_payment_failure_notification` —
the fan-out helper that pushes a payment-failure alert to every device
the user has registered.

Goals:
  - Happy path: every registered device gets a publish call carrying
    the expected title/body/data payload.
  - No registered devices: helper exits cleanly (returns None, no
    exceptions). Most failed renewals will land here because not every
    user opts in to notifications.
  - Per-device SNS failure: one disabled endpoint must NOT block the
    other devices in the user's fan-out set.
  - Token fetch failure: never bubbles — the caller already persisted
    the renewal failure to DynamoDB, so a notification glitch must not
    cause the webhook to retry.
  - Payload contract: `data.type == 'payment_failed'` and
    `data.deep_link == 'your_subscription'` so the client can branch.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


def _build_service():
    """Stub a SubscriptionService with mockable dynamodb + SNS dependencies."""
    from src.app.services.subscription_service import SubscriptionService

    dynamodb = AsyncMock()
    svc = SubscriptionService(dynamodb)
    # Replace the real SNSService with a MagicMock — publish_to_endpoint
    # is a sync method, so MagicMock (not AsyncMock) is correct here.
    svc.sns_service = MagicMock()
    svc.sns_service.publish_to_endpoint = MagicMock(return_value="msg-1")
    return svc, dynamodb


@pytest.mark.asyncio
async def test_dispatches_to_every_registered_device():
    svc, dynamodb = _build_service()
    dynamodb.get_user_device_tokens = AsyncMock(
        return_value=[
            {"endpoint_arn": "arn:ios-1", "platform": "ios"},
            {"endpoint_arn": "arn:android-1", "platform": "android"},
            {"endpoint_arn": "arn:ios-2", "platform": "ios"},
        ]
    )

    await svc._send_payment_failure_notification(
        user_id="user-1",
        subscription_id="sub-123",
    )

    assert svc.sns_service.publish_to_endpoint.call_count == 3
    # All calls should carry the same payment_failed payload.
    for call in svc.sns_service.publish_to_endpoint.call_args_list:
        kwargs = call.kwargs
        assert kwargs["data"]["type"] == "payment_failed"
        assert kwargs["data"]["subscription_id"] == "sub-123"
        assert kwargs["data"]["deep_link"] == "your_subscription"
        assert "Payment" in kwargs["title"]
        assert "payment method" in kwargs["body"].lower()


@pytest.mark.asyncio
async def test_no_registered_devices_is_a_clean_noop():
    svc, dynamodb = _build_service()
    dynamodb.get_user_device_tokens = AsyncMock(return_value=[])

    # Should NOT raise even though we have no fan-out targets.
    await svc._send_payment_failure_notification(
        user_id="user-1",
        subscription_id="sub-123",
    )

    svc.sns_service.publish_to_endpoint.assert_not_called()


@pytest.mark.asyncio
async def test_one_bad_device_does_not_block_the_rest():
    """A disabled endpoint or transient SNS error on device A must not
    prevent device B from receiving the push.
    """
    svc, dynamodb = _build_service()
    dynamodb.get_user_device_tokens = AsyncMock(
        return_value=[
            {"endpoint_arn": "arn:dead", "platform": "ios"},
            {"endpoint_arn": "arn:ok", "platform": "android"},
        ]
    )

    # First device raises an unexpected error; second succeeds.
    svc.sns_service.publish_to_endpoint = MagicMock(
        side_effect=[RuntimeError("boom"), "msg-2"]
    )

    await svc._send_payment_failure_notification(
        user_id="user-1",
        subscription_id="sub-123",
    )

    assert svc.sns_service.publish_to_endpoint.call_count == 2


@pytest.mark.asyncio
async def test_token_fetch_failure_does_not_bubble():
    """If get_user_device_tokens raises, the helper swallows it. The
    renewal failure is already persisted upstream — a notification
    glitch must not cause the webhook handler to error.
    """
    svc, dynamodb = _build_service()
    dynamodb.get_user_device_tokens = AsyncMock(
        side_effect=RuntimeError("dynamodb is down")
    )

    # Should not raise.
    await svc._send_payment_failure_notification(
        user_id="user-1",
        subscription_id="sub-123",
    )

    svc.sns_service.publish_to_endpoint.assert_not_called()


@pytest.mark.asyncio
async def test_skips_records_missing_endpoint_arn():
    """Token row without endpoint_arn (e.g., partial save) is skipped
    rather than passed to SNS where it would 400."""
    svc, dynamodb = _build_service()
    dynamodb.get_user_device_tokens = AsyncMock(
        return_value=[
            {"endpoint_arn": "arn:good", "platform": "ios"},
            {"platform": "android"},  # malformed — no endpoint_arn
            {"endpoint_arn": "", "platform": "ios"},  # empty string
        ]
    )

    await svc._send_payment_failure_notification(
        user_id="user-1",
        subscription_id="sub-123",
    )

    # Only the well-formed record dispatched.
    assert svc.sns_service.publish_to_endpoint.call_count == 1
