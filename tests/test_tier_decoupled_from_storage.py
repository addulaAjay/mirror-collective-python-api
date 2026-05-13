"""
Regression tests for the tier / storage-add-on decoupling
(pricing spec 2026-05-12).

Before this refactor the codebase would promote `subscription_tier` from
`"core"` to `"core_plus"` when a user bought the storage add-on, and
that promoted value would persist in DynamoDB. When the future "Plus"
premium tier launches, a value like `"plus"` would collide with the
already-persisted `"core_plus"` rows.

The fix keeps tier orthogonal to the +100 GB upgrade:
  - `subscription_tier ∈ {free, trial, basic, plus(future)}` —
    reflects what the user pays for.
  - `storage_add_on_active: bool` — the ONLY signal for the +100 GB
    upgrade. Tier never carries the storage signal.

These tests pin the contract so a future regression can't quietly
re-couple them.
"""

from unittest.mock import AsyncMock

import pytest


def _build_service():
    from src.app.services.subscription_service import SubscriptionService

    dynamodb = AsyncMock()
    return SubscriptionService(dynamodb), dynamodb


@pytest.mark.asyncio
async def test_storage_addon_purchase_does_not_promote_tier():
    """User already on Basic buys the +100 GB add-on. Tier stays 'basic';
    storage_add_on_active flips to True; quota becomes 150 GB.
    """
    from src.app.models.subscription import (
        BillingPeriod,
        Platform,
        Subscription,
        SubscriptionStatus,
        SubscriptionType,
    )
    from src.app.models.user_profile import UserProfile, UserStatus

    svc, dynamodb = _build_service()

    existing = UserProfile(
        user_id="u1",
        email="u1@example.com",
        subscription_status="active",
        subscription_tier="basic",
        echo_vault_quota_gb=50.0,
        storage_add_on_active=False,
        status=UserStatus.CONFIRMED,
    )
    dynamodb.get_user_profile = AsyncMock(return_value=existing)
    dynamodb.update_user_profile = AsyncMock(return_value=existing)

    storage_sub = Subscription(
        user_id="u1",
        subscription_id="storage-ot-1",
        product_id="com.themirrorcollective.mirror.storage.monthly",
        subscription_type=SubscriptionType.STORAGE_ADD_ON,
        platform=Platform.IOS,
        status=SubscriptionStatus.ACTIVE,
        billing_period=BillingPeriod.MONTHLY,
        price_usd=4.99,
    )

    await svc._update_user_subscription_status("u1", storage_sub)

    dynamodb.update_user_profile.assert_awaited_once()
    await_args = dynamodb.update_user_profile.await_args
    assert await_args is not None
    updated = await_args.args[0]

    # Tier MUST remain "basic" — no implicit promotion to "core_plus" /
    # "basic_plus" / anything else.
    assert updated.subscription_tier == "basic"
    assert updated.storage_add_on_active is True
    assert updated.echo_vault_quota_gb == 150.0


@pytest.mark.asyncio
async def test_basic_purchase_sets_tier_to_basic_not_core():
    """Activating the core (now Basic) subscription writes tier='basic'."""
    from src.app.models.subscription import (
        BillingPeriod,
        Platform,
        Subscription,
        SubscriptionStatus,
        SubscriptionType,
    )
    from src.app.models.user_profile import UserProfile, UserStatus

    svc, dynamodb = _build_service()

    profile = UserProfile(
        user_id="u1",
        email="u1@example.com",
        subscription_status="none",
        subscription_tier="free",
        status=UserStatus.CONFIRMED,
    )
    dynamodb.get_user_profile = AsyncMock(return_value=profile)
    dynamodb.update_user_profile = AsyncMock(return_value=profile)

    basic_sub = Subscription(
        user_id="u1",
        subscription_id="ot1",
        product_id="com.themirrorcollective.mirror.core.monthly",
        subscription_type=SubscriptionType.MIRROR_BASIC,
        platform=Platform.IOS,
        status=SubscriptionStatus.ACTIVE,
        billing_period=BillingPeriod.MONTHLY,
        price_usd=9.99,
    )

    await svc._update_user_subscription_status("u1", basic_sub)

    await_args = dynamodb.update_user_profile.await_args
    assert await_args is not None
    updated = await_args.args[0]
    assert updated.subscription_tier == "basic"
    # Without the add-on the quota stays at the 50 GB baseline.
    assert updated.echo_vault_quota_gb == 50.0
    assert updated.storage_add_on_active is False


@pytest.mark.asyncio
async def test_subscription_type_enum_has_basic_value():
    """Catches a rename regression — the wire value must stay 'basic'
    (used in DynamoDB rows + receipt parsing).
    """
    from src.app.models.subscription import SubscriptionType

    assert SubscriptionType.MIRROR_BASIC.value == "basic"
    assert not hasattr(SubscriptionType, "MIRROR_CORE")
