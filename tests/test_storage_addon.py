"""Storage add-on: entitlement math + lifecycle.

The add-on is an OVERLAY on Core (requires active Core). tier + quota are
derived idempotently from flags via UserProfile.recompute_entitlement(); the
webhook/verify handlers just flip the flags. Buying, renewing, and ending the
add-on must not double-count quota, must keep Core when only the add-on ends,
and ending Core must also drop the add-on.
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
from src.app.models.user_profile import UserProfile
from src.app.services.subscription_service import SubscriptionService


def _profile(**kw) -> UserProfile:
    return UserProfile(user_id="u1", email="u1@example.com", **kw)


def _core_active_profile() -> UserProfile:
    return _profile(
        subscription_status="active",
        subscription_tier="core",
        primary_subscription_id="core-txn",
        echo_vault_quota_gb=50.0,
    )


def _sub(sub_type: SubscriptionType, sid: str) -> Subscription:
    return Subscription(
        user_id="u1",
        subscription_id=sid,
        product_id="com.themirrorcollective.mirror."
        + (
            "storage.monthly"
            if sub_type == SubscriptionType.STORAGE_ADD_ON
            else "monthly"
        ),
        subscription_type=sub_type,
        platform=Platform.IOS,
        status=SubscriptionStatus.ACTIVE,
        billing_period=BillingPeriod.MONTHLY,
        price_usd=1.99,
        purchase_date="2026-09-01T00:00:00Z",
        expiry_date="2026-10-01T00:00:00Z",
        auto_renew_enabled=True,
        receipt_data="r",
    )


# ── recompute_entitlement: the single source of truth ─────────────────────────
def test_no_core_is_free_zero():
    p = _profile(primary_subscription_id=None, storage_add_on_active=True)
    p.recompute_entitlement()
    assert p.subscription_tier == "free"
    assert p.echo_vault_quota_gb == 0.0  # add-on grants nothing without Core


def test_core_only_is_50():
    p = _core_active_profile()
    p.recompute_entitlement()
    assert p.subscription_tier == "core"
    assert p.echo_vault_quota_gb == 50.0


def test_core_plus_addon_is_150():
    p = _core_active_profile()
    p.storage_add_on_active = True
    p.recompute_entitlement()
    assert p.subscription_tier == "core_plus"
    assert p.echo_vault_quota_gb == 150.0


def test_trial_is_50():
    p = _profile(subscription_status="trial", primary_subscription_id="core-txn")
    p.recompute_entitlement()
    assert p.subscription_tier == "trial"
    assert p.echo_vault_quota_gb == 50.0


# ── Lifecycle via the service ─────────────────────────────────────────────────
def _svc_with_profile(profile):
    db = AsyncMock()
    db.get_user_profile = AsyncMock(return_value=profile)
    db.update_user_profile = AsyncMock()
    db.query_items = AsyncMock(return_value=[])  # no other subscriptions
    return SubscriptionService(db), db


@pytest.mark.asyncio
async def test_buy_addon_grants_150_core_plus():
    profile = _core_active_profile()
    svc, _ = _svc_with_profile(profile)
    await svc._update_user_subscription_status(
        "u1", _sub(SubscriptionType.STORAGE_ADD_ON, "storage-txn")
    )
    assert profile.storage_add_on_active is True
    assert profile.storage_subscription_id == "storage-txn"
    assert profile.subscription_tier == "core_plus"
    assert profile.echo_vault_quota_gb == 150.0
    # The add-on must NOT change the Core status.
    assert profile.subscription_status == "active"


@pytest.mark.asyncio
async def test_addon_renewal_does_not_double_count():
    profile = _core_active_profile()
    profile.storage_add_on_active = True
    profile.storage_subscription_id = "storage-txn"
    profile.subscription_tier = "core_plus"
    profile.echo_vault_quota_gb = 150.0
    svc, _ = _svc_with_profile(profile)
    # Re-verify / renew the add-on twice — quota must stay 150, never 250/350.
    for _ in range(2):
        await svc._update_user_subscription_status(
            "u1", _sub(SubscriptionType.STORAGE_ADD_ON, "storage-txn")
        )
    assert profile.echo_vault_quota_gb == 150.0


@pytest.mark.asyncio
async def test_addon_ends_keeps_core_drops_to_50():
    profile = _core_active_profile()
    profile.storage_add_on_active = True
    profile.storage_subscription_id = "storage-txn"
    profile.subscription_tier = "core_plus"
    profile.echo_vault_quota_gb = 150.0
    svc, _ = _svc_with_profile(profile)
    await svc._revoke_profile_access(
        _sub(SubscriptionType.STORAGE_ADD_ON, "storage-txn")
    )
    assert profile.storage_add_on_active is False
    assert profile.storage_subscription_id is None
    assert profile.subscription_tier == "core"  # Core retained
    assert profile.echo_vault_quota_gb == 50.0
    assert profile.primary_subscription_id == "core-txn"


@pytest.mark.asyncio
async def test_core_ends_also_drops_addon():
    profile = _core_active_profile()
    profile.storage_add_on_active = True
    profile.storage_subscription_id = "storage-txn"
    profile.subscription_tier = "core_plus"
    profile.echo_vault_quota_gb = 150.0
    svc, _ = _svc_with_profile(profile)
    await svc._revoke_profile_access(_sub(SubscriptionType.MIRROR_CORE, "core-txn"))
    assert profile.subscription_status == "expired"
    assert profile.primary_subscription_id is None
    # Ending Core ends the add-on too (add-on requires Core).
    assert profile.storage_add_on_active is False
    assert profile.storage_subscription_id is None
    assert profile.subscription_tier == "free"
    assert profile.echo_vault_quota_gb == 0.0


def test_parse_product_id_storage_vs_core():
    svc, _ = _svc_with_profile(_core_active_profile())
    assert svc._parse_product_id("com.themirrorcollective.mirror.storage.monthly")[
        0
    ] == (SubscriptionType.STORAGE_ADD_ON)
    assert svc._parse_product_id("com.themirrorcollective.mirror.monthly")[0] == (
        SubscriptionType.MIRROR_CORE
    )
    assert svc._parse_product_id("com.themirrorcollective.mirror.yearly")[0] == (
        SubscriptionType.MIRROR_CORE
    )


def test_existing_core_user_without_primary_id_not_downgraded():
    """Backward-compat: an existing active-Core user whose primary_subscription_id
    was never populated must stay core/50 — driven by status, not the id ref."""
    p = _profile(subscription_status="active", primary_subscription_id=None)
    p.recompute_entitlement()
    assert p.subscription_tier == "core"
    assert p.echo_vault_quota_gb == 50.0
