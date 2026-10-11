"""A StoreKit trial must record when it ends.

Production, 2026-10-11: a user on an Apple intro-offer trial could not use
Echo Vault at all. Their profile read subscription_status="trial" and
subscription_tier="trial" — entitled by every outward sign — but
trial_expires_at was absent, and the entitlement guard treats a trial with no
expiry as lapsed (``_trial_has_lapsed`` fails closed).

Two components each held a defensible rule and together contradicted:

  recompute_entitlement()   status "trial" -> tier "trial" -> entitled
  _profile_is_entitled()    tier "trial" + no expiry -> lapsed -> 403

``trial_management_service.start_trial()`` (the in-app path) always wrote the
timestamp. The StoreKit path never did, so every Apple trial conversion landed
in the contradictory state — silently locking paying users out of the feature
they had just started paying for.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.app.core import entitlements as ent
from src.app.models.subscription import Platform, SubscriptionType
from src.app.services.subscription_service import SubscriptionService


def a_profile():
    """Profile shaped like the real one, before activation."""
    return SimpleNamespace(
        user_id="u1",
        subscription_status="none",
        subscription_tier="free",
        trial_started_at=None,
        trial_expires_at=None,
        has_used_trial=False,
        storage_add_on_active=False,
        primary_subscription_id=None,
        storage_subscription_id=None,
        echo_vault_quota_gb=0.0,
        last_subscription_check=None,
        recompute_entitlement=lambda: None,
    )


def a_trial_subscription(**overrides):
    payload = dict(
        subscription_id="2000001216327774",
        subscription_type=SubscriptionType.MIRROR_CORE,
        platform=Platform.IOS,
        is_in_trial=True,
        trial_start_date="2026-09-13T17:34:47Z",
        trial_end_date="2026-09-27T17:34:47Z",
        purchase_date="2026-09-13T17:34:47Z",
        expiry_date="2026-09-27T17:34:47Z",
    )
    payload.update(overrides)
    return SimpleNamespace(**payload)


async def activate(profile, subscription):
    db = AsyncMock()
    db.get_user_profile = AsyncMock(return_value=profile)
    db.update_user_profile = AsyncMock(return_value=profile)
    svc = SubscriptionService(db)
    await svc._update_user_subscription_status("u1", subscription)
    return profile


class TestTrialActivationRecordsTheWindow:
    async def test_writes_trial_expires_at(self):
        # The bug: this was absent, so the guard read the trial as lapsed.
        profile = await activate(a_profile(), a_trial_subscription())

        assert profile.trial_expires_at == "2026-09-27T17:34:47Z"

    async def test_writes_trial_started_at(self):
        profile = await activate(a_profile(), a_trial_subscription())

        assert profile.trial_started_at == "2026-09-13T17:34:47Z"

    async def test_still_sets_status_and_has_used_trial(self):
        profile = await activate(a_profile(), a_trial_subscription())

        assert profile.subscription_status == "trial"
        assert profile.has_used_trial is True

    async def test_falls_back_to_expiry_date_when_trial_end_is_missing(self):
        # Apple does not always populate the trial window separately; for an
        # intro offer the subscription's expiry IS the trial end.
        profile = await activate(
            a_profile(),
            a_trial_subscription(
                trial_end_date=None, expiry_date="2026-10-01T00:00:00Z"
            ),
        )

        assert profile.trial_expires_at == "2026-10-01T00:00:00Z"

    async def test_falls_back_to_purchase_date_for_trial_start(self):
        profile = await activate(
            a_profile(),
            a_trial_subscription(
                trial_start_date=None, purchase_date="2026-09-14T00:00:00Z"
            ),
        )

        assert profile.trial_started_at == "2026-09-14T00:00:00Z"

    async def test_leaves_expiry_unset_when_apple_gives_no_dates(self):
        # Nothing is invented. The guard stays fail-closed, which is correct —
        # we simply have no evidence the trial is live.
        profile = await activate(
            a_profile(),
            a_trial_subscription(trial_end_date=None, expiry_date=None),
        )

        assert profile.trial_expires_at is None


class TestPaidActivationUnaffected:
    async def test_a_paid_purchase_sets_active_not_trial(self):
        profile = await activate(
            a_profile(),
            a_trial_subscription(is_in_trial=False),
        )

        assert profile.subscription_status == "active"

    async def test_a_paid_purchase_does_not_write_trial_dates(self):
        # Paid tiers are governed by the subscription lifecycle, not the trial
        # timestamp; writing one would be misleading.
        profile = await activate(
            a_profile(),
            a_trial_subscription(is_in_trial=False),
        )

        assert profile.trial_expires_at is None
        assert profile.trial_started_at is None

    async def test_a_storage_addon_does_not_touch_trial_dates(self):
        profile = await activate(
            a_profile(),
            a_trial_subscription(subscription_type=SubscriptionType.STORAGE_ADD_ON),
        )

        assert profile.trial_expires_at is None
        assert profile.storage_add_on_active is True


class TestTheGuardNowAgrees:
    """The point of the fix: both components reach the same verdict."""

    def test_a_trial_with_a_future_expiry_is_entitled(self):
        from datetime import datetime, timedelta, timezone

        future = (
            (datetime.now(timezone.utc) + timedelta(days=7))
            .isoformat()
            .replace("+00:00", "Z")
        )
        profile = SimpleNamespace(subscription_tier="trial", trial_expires_at=future)

        assert ent._profile_is_entitled(profile) is True

    def test_a_trial_with_NO_expiry_is_still_blocked(self):
        # Unchanged, and deliberately so — the guard stays fail-closed. The fix
        # is that activation no longer produces this state, not that the guard
        # started trusting it.
        profile = SimpleNamespace(subscription_tier="trial", trial_expires_at=None)

        assert ent._profile_is_entitled(profile) is False

    def test_the_production_profile_shape_was_blocked(self):
        # Exactly what user 0428f478 looked like: every outward sign of an
        # active trial, locked out by the missing timestamp.
        profile = SimpleNamespace(
            subscription_tier="trial",
            subscription_status="trial",
            trial_expires_at=None,
        )

        assert ent._profile_is_entitled(profile) is False
