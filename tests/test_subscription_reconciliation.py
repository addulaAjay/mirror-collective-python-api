"""Subscription reconciliation cron.

The daily backstop for missed/delayed App Store Server Notifications:
``reconcile_subscriptions`` scans active-ish subs, fetches Apple's authoritative
status (Get All Subscription Statuses), and corrects DB drift through the same
mutation + downgrade paths the webhooks use.

Covered:
- Apple EXPIRED  -> mark expired  + revoke access
- Apple REVOKED  -> mark refunded + revoke access
- Apple GRACE/RETRY -> mark grace_period (access kept)
- Apple ACTIVE + stale expiry -> advance expiry (never regress)
- Apple ACTIVE + current expiry -> no write
- already-correct DB status -> skipped
- non-iOS / unknown Apple state -> untouched
- one failing sub is isolated (errors counter), batch continues
"""

from unittest.mock import AsyncMock

import pytest

from src.app.models.subscription import (
    BillingPeriod,
    Platform,
    Subscription,
    SubscriptionStatus,
    SubscriptionType,
)
from src.app.models.user_profile import UserProfile, UserStatus
from src.app.services.subscription_service import SubscriptionService, _ms_to_iso


def _item(
    otid="1000000000000001",
    status=SubscriptionStatus.ACTIVE,
    expiry="2026-12-13T00:00:00Z",
    sub_type=SubscriptionType.MIRROR_CORE,
    platform=Platform.IOS,
):
    return Subscription(
        user_id="u1",
        subscription_id=otid,
        product_id="com.themirrorcollective.mirror.monthly",
        subscription_type=sub_type,
        platform=platform,
        status=status,
        billing_period=BillingPeriod.MONTHLY,
        price_usd=9.99,
        purchase_date="2026-09-13T00:00:00Z",
        expiry_date=expiry,
        auto_renew_enabled=True,
        receipt_data="r",
    ).to_dynamodb_item()


def _profile():
    return UserProfile(
        user_id="u1",
        email="u1@example.com",
        status=UserStatus.CONFIRMED,
        subscription_status="active",
        primary_subscription_id="1000000000000001",
        subscription_tier="core",
        echo_vault_quota_gb=50.0,
    )


def _service(items, apple_state):
    db = AsyncMock()
    db.scan_items = AsyncMock(return_value=items)
    db.update_item = AsyncMock(return_value=True)
    db.put_item = AsyncMock()
    db.get_user_profile = AsyncMock(return_value=_profile())
    db.update_user_profile = AsyncMock()
    db.query_items = AsyncMock(return_value=[])  # no other active core
    svc = SubscriptionService(db)
    setattr(
        svc.receipt_validator,
        "get_apple_subscription_state",
        AsyncMock(return_value=apple_state),
    )
    return svc


def _set_fields(update_item_mock):
    """Extract the {field: value} applied by _ordered_subscription_set from the
    most recent update_item call (aliased via ExpressionAttributeNames)."""
    call = update_item_mock.await_args
    names = call.kwargs["expression_names"]
    values = call.args[3]
    fields = {}
    for i, alias in enumerate(sorted(names)):  # #f0, #f1, ...
        fields[names[f"#f{i}"]] = values[f":v{i}"]
    return fields


@pytest.mark.asyncio
async def test_apple_expired_marks_expired_and_revokes():
    svc = _service([_item(status=SubscriptionStatus.ACTIVE)], {"status": 2})

    result = await svc.reconcile_subscriptions()

    assert result == {"checked": 1, "corrected": 1, "errors": 0}
    fields = _set_fields(svc.dynamodb_service.update_item)
    assert fields["status"] == "expired"
    assert fields["auto_renew_enabled"] is False
    # Access revoked via profile update.
    svc.dynamodb_service.update_user_profile.assert_awaited()


@pytest.mark.asyncio
async def test_apple_revoked_marks_refunded_and_revokes():
    svc = _service([_item(status=SubscriptionStatus.ACTIVE)], {"status": 5})

    result = await svc.reconcile_subscriptions()

    assert result["corrected"] == 1
    fields = _set_fields(svc.dynamodb_service.update_item)
    assert fields["status"] == "refunded"
    svc.dynamodb_service.update_user_profile.assert_awaited()


@pytest.mark.asyncio
async def test_apple_grace_marks_grace_period_without_revoke():
    svc = _service([_item(status=SubscriptionStatus.ACTIVE)], {"status": 4})

    result = await svc.reconcile_subscriptions()

    assert result["corrected"] == 1
    fields = _set_fields(svc.dynamodb_service.update_item)
    assert fields["status"] == "grace_period"
    # Grace still grants access — profile is not revoked.
    svc.dynamodb_service.update_user_profile.assert_not_awaited()


@pytest.mark.asyncio
async def test_apple_active_advances_stale_expiry():
    # DB expiry is in the past; Apple's latest verified expiry is later.
    stale = "2026-01-01T00:00:00Z"
    later_ms = 1798761600000  # ~2027-01-01
    svc = _service(
        [_item(status=SubscriptionStatus.ACTIVE, expiry=stale)],
        {"status": 1, "expires_date_ms": later_ms},
    )

    result = await svc.reconcile_subscriptions()

    assert result["corrected"] == 1
    fields = _set_fields(svc.dynamodb_service.update_item)
    assert fields["expiry_date"] == _ms_to_iso(later_ms)


@pytest.mark.asyncio
async def test_apple_active_does_not_regress_expiry():
    # Apple reports an OLDER expiry than what we have stored — never regress.
    current = "2026-12-13T00:00:00Z"
    older_ms = 1704067200000  # 2024-01-01
    svc = _service(
        [_item(status=SubscriptionStatus.ACTIVE, expiry=current)],
        {"status": 1, "expires_date_ms": older_ms},
    )

    result = await svc.reconcile_subscriptions()

    assert result["corrected"] == 0
    svc.dynamodb_service.update_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_expired_db_status_is_skipped():
    # DB not scanned into the batch would be normal, but if a still-active-ish
    # record already matches Apple's expired verdict, don't double-correct.
    svc = _service([_item(status=SubscriptionStatus.EXPIRED)], {"status": 2})

    result = await svc.reconcile_subscriptions()

    assert result == {"checked": 1, "corrected": 0, "errors": 0}
    svc.dynamodb_service.update_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_ios_subscription_is_skipped():
    svc = _service([_item(platform=Platform.ANDROID)], {"status": 2})

    result = await svc.reconcile_subscriptions()

    assert result["checked"] == 0
    svc.receipt_validator.get_apple_subscription_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_apple_state_leaves_db_untouched():
    svc = _service([_item(status=SubscriptionStatus.ACTIVE)], None)

    result = await svc.reconcile_subscriptions()

    assert result == {"checked": 1, "corrected": 0, "errors": 0}
    svc.dynamodb_service.update_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_failing_sub_is_isolated():
    svc = _service(
        [
            _item(otid="1000000000000001", status=SubscriptionStatus.ACTIVE),
            _item(otid="1000000000000002", status=SubscriptionStatus.ACTIVE),
        ],
        {"status": 2},
    )
    # First lookup raises, second returns a clean expired verdict.
    setattr(
        svc.receipt_validator,
        "get_apple_subscription_state",
        AsyncMock(side_effect=[RuntimeError("apple 500"), {"status": 2}]),
    )

    result = await svc.reconcile_subscriptions()

    assert result["checked"] == 2
    assert result["errors"] == 1
    assert result["corrected"] == 1  # the healthy one still processed
