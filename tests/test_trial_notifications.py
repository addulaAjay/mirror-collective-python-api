"""Trial-expiry push nudges: dispatch + wiring.

Regression coverage for the gap where trial nudges were dead code — the job
constructed ``TrialManagementService`` with no push service, so warnings and the
expiry notice were silently dropped. These tests lock in that (a) the concrete
``PushNotificationService`` fans a push out to a user's active endpoints, and
(b) the trial service actually invokes it.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.app.services.push_notification_service import PushNotificationService
from src.app.services.trial_management_service import TrialManagementService


def _tokens(*specs):
    """specs: (endpoint_arn, is_active) tuples -> device-token records."""
    return [{"endpoint_arn": arn, "is_active": active} for arn, active in specs]


# --------------------------------------------------------------- push dispatch
@pytest.mark.asyncio
async def test_push_fans_out_to_active_endpoints_only():
    db: Any = SimpleNamespace(
        get_user_device_tokens=AsyncMock(
            return_value=_tokens(("arn-a", True), ("arn-b", False), ("arn-c", True))
        )
    )
    sns: Any = SimpleNamespace(
        publish_to_endpoint_async=AsyncMock(return_value="msg-id")
    )
    svc = PushNotificationService(db, sns)

    delivered = await svc.send_notification("u1", "Title", "Body", {"type": "t"})

    assert delivered == 2  # arn-b is inactive -> skipped
    called_arns = {c.args[0] for c in sns.publish_to_endpoint_async.call_args_list}
    assert called_arns == {"arn-a", "arn-c"}


@pytest.mark.asyncio
async def test_push_returns_zero_when_no_tokens():
    db: Any = SimpleNamespace(get_user_device_tokens=AsyncMock(return_value=[]))
    sns: Any = SimpleNamespace(publish_to_endpoint_async=AsyncMock())
    svc = PushNotificationService(db, sns)

    delivered = await svc.send_notification("u1", "T", "B")

    assert delivered == 0
    sns.publish_to_endpoint_async.assert_not_called()


@pytest.mark.asyncio
async def test_push_one_bad_endpoint_does_not_abort_batch():
    db: Any = SimpleNamespace(
        get_user_device_tokens=AsyncMock(
            return_value=_tokens(("arn-a", True), ("arn-b", True))
        )
    )
    sns: Any = SimpleNamespace(
        publish_to_endpoint_async=AsyncMock(side_effect=[RuntimeError("boom"), "ok"])
    )
    svc = PushNotificationService(db, sns)

    delivered = await svc.send_notification("u1", "T", "B")

    assert delivered == 1  # first raised, second still delivered


# ------------------------------------------------------------- trial wiring
@pytest.mark.asyncio
async def test_expiry_warning_sends_push_and_records():
    push = SimpleNamespace(send_notification=AsyncMock(return_value=1))
    db = SimpleNamespace(update_user_profile=AsyncMock())
    svc = TrialManagementService(db, push)
    profile = SimpleNamespace(user_id="u1", trial_notifications_sent=[])

    ok = await svc.send_trial_expiration_notification(profile, days_remaining=3)

    assert ok is True
    push.send_notification.assert_awaited_once()
    kwargs = push.send_notification.await_args.kwargs
    assert kwargs["user_id"] == "u1"
    assert kwargs["data"]["screen"] == "StartFreeTrial"
    assert "3_day" in profile.trial_notifications_sent  # dedupe marker recorded


@pytest.mark.asyncio
async def test_expiry_warning_not_resent_when_already_recorded():
    push = SimpleNamespace(send_notification=AsyncMock(return_value=1))
    db = SimpleNamespace(update_user_profile=AsyncMock())
    svc = TrialManagementService(db, push)
    profile = SimpleNamespace(user_id="u1", trial_notifications_sent=["7_day"])

    await svc.send_trial_expiration_notification(profile, days_remaining=7)

    # It still dispatches, but must not duplicate the marker or re-persist it.
    assert profile.trial_notifications_sent == ["7_day"]
    db.update_user_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_expired_locks_vault_and_nudges():
    profile = SimpleNamespace(
        user_id="u1",
        primary_subscription_id=None,
        subscription_status="trial",
        subscription_tier="trial",
        echo_vault_quota_gb=50.0,
        trial_notifications_sent=[],
    )
    push = SimpleNamespace(send_notification=AsyncMock(return_value=1))
    db = SimpleNamespace(
        get_user_profile=AsyncMock(return_value=profile),
        update_user_profile=AsyncMock(),
    )
    svc = TrialManagementService(db, push)

    result = await svc.handle_trial_expired("u1")

    assert result["subscription_status"] == "trial_expired"
    assert profile.subscription_tier == "free"
    assert profile.echo_vault_quota_gb == 0.0  # vault locked
    push.send_notification.assert_awaited_once()
    assert push.send_notification.await_args.kwargs["data"]["type"] == "trial_expired"
    assert "expired" in profile.trial_notifications_sent


@pytest.mark.asyncio
async def test_trial_expired_no_downgrade_when_paid_subscription():
    profile = SimpleNamespace(
        user_id="u1",
        primary_subscription_id="sub-123",
        subscription_status="trial",
        subscription_tier="core",
        echo_vault_quota_gb=50.0,
        trial_notifications_sent=[],
    )
    push = SimpleNamespace(send_notification=AsyncMock(return_value=1))
    db = SimpleNamespace(
        get_user_profile=AsyncMock(return_value=profile),
        update_user_profile=AsyncMock(),
    )
    svc = TrialManagementService(db, push)

    result = await svc.handle_trial_expired("u1")

    assert result["action"] == "no_change"
    push.send_notification.assert_not_awaited()  # paid users are never nudged
    assert profile.subscription_tier == "core"  # untouched
