"""
Tests for the feature-flag scaffolding (pricing spec 2026-05-12 §6, §12).

Pins three contracts so a future regression can't quietly widen or
narrow access:

  1. `require_feature(Feature.BASIC_ACCESS)` behaves equivalently to
     the legacy `require_entitled` for every interesting status × tier
     combination. New code points at the factory; existing routes
     keep working via the back-compat alias.
  2. Plus-only features (PLUS_ACCESS and friends) deny basic-tier
     users with a feature-specific 402 reason payload — the client
     uses this to route to the Plus upsell paywall.
  3. The status gate is independent of the feature gate — a Plus
     user whose subscription_status is `trial_expired` gets 402
     reason="trial_expired", NOT a feature-locked reason. Status
     comes first because re-subscribing fixes status, but doesn't
     change tier.

These tests touch the FastAPI dependency directly (no full ASGI app)
so the 402 surface is asserted on HTTPException, not on a transport
response. The existing test_entitlement.py covers the integration
path; this file pins the per-feature factory logic.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.app.core.entitlement import EntitledUser, require_entitled, require_feature
from src.app.core.features import FEATURE_TIER_MAP, Feature, tier_grants
from src.app.models.user_profile import UserProfile, UserStatus

_USER_CLAIMS: Dict[str, Any] = {
    "sub": "test-user-1",
    "id": "test-user-1",
    "email": "u1@example.com",
}


def _make_profile(*, status: str, tier: str) -> UserProfile:
    return UserProfile(
        user_id="test-user-1",
        email="u1@example.com",
        subscription_status=status,
        subscription_tier=tier,
        status=UserStatus.CONFIRMED,
    )


def _install_profile(monkeypatch, profile):
    """Stub the module-level DynamoDB lookup so the gate runs without AWS."""
    from src.app.core import entitlement as ent_mod

    fake = AsyncMock()
    fake.get_user_profile = AsyncMock(return_value=profile)
    monkeypatch.setattr(ent_mod, "_dynamodb_service", fake)
    monkeypatch.setattr(ent_mod, "_get_dynamodb_service", lambda: fake)
    return fake


# --------------------------------------------------------------------------- #
# Catalog sanity
# --------------------------------------------------------------------------- #


class TestFeatureCatalog:
    def test_every_feature_has_a_tier_set(self):
        """A missing entry in FEATURE_TIER_MAP is a KeyError at request
        time — guard against silent regressions when adding new
        features."""
        for feature in Feature:
            assert feature in FEATURE_TIER_MAP, (
                f"{feature} missing from FEATURE_TIER_MAP — adding a "
                f"Feature without a tier set means routes that gate on "
                f"it will 500 instead of 402."
            )

    def test_basic_access_includes_trial_and_basic(self):
        """Launch entitlement matrix: trial users get full Basic; basic
        and plus tiers obviously get it too."""
        assert "trial" in FEATURE_TIER_MAP[Feature.BASIC_ACCESS]
        assert "basic" in FEATURE_TIER_MAP[Feature.BASIC_ACCESS]
        assert "plus" in FEATURE_TIER_MAP[Feature.BASIC_ACCESS]

    def test_plus_features_exclude_basic(self):
        """Spec §10: future Plus features (Reflection Room, etc.) must
        NOT be granted by the basic tier. This pin breaks if someone
        mistakenly widens the tier set during a refactor."""
        plus_only_features = [
            Feature.PLUS_ACCESS,
            Feature.REFLECTION_ROOM_ACCESS,
            Feature.ECHO_SIGNATURE_ACCESS,
            Feature.MIRROR_MOMENT_ACCESS,
            Feature.MIRROR_PLEDGE_ACCESS,
            Feature.CODE_LIBRARY_ACCESS,
            Feature.MEMORY_TIMELINE_ACCESS,
            Feature.ROLE_PATTERN_TIMELINE_ACCESS,
        ]
        for feature in plus_only_features:
            assert (
                "basic" not in FEATURE_TIER_MAP[feature]
            ), f"{feature} must not be granted by basic tier"
            assert (
                "trial" not in FEATURE_TIER_MAP[feature]
            ), f"{feature} must not be granted by trial tier"

    def test_tier_grants_predicate(self):
        assert tier_grants(Feature.BASIC_ACCESS, "basic")
        assert tier_grants(Feature.BASIC_ACCESS, "trial")
        assert not tier_grants(Feature.PLUS_ACCESS, "basic")
        assert tier_grants(Feature.PLUS_ACCESS, "plus")
        assert not tier_grants(Feature.BASIC_ACCESS, "free")


# --------------------------------------------------------------------------- #
# require_feature(BASIC_ACCESS) ≡ require_entitled (back-compat)
# --------------------------------------------------------------------------- #


class TestBasicAccessGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["trial", "active", "grace_period"])
    async def test_passes_for_entitled_status_and_basic_tier(self, monkeypatch, status):
        _install_profile(monkeypatch, _make_profile(status=status, tier="basic"))
        dep = require_feature(Feature.BASIC_ACCESS)
        result = await dep(_USER_CLAIMS)
        assert isinstance(result, EntitledUser)
        assert result.profile.subscription_status == status

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,expected_reason",
        [
            ("trial_expired", "trial_expired"),
            ("expired", "expired"),
            ("cancelled", "expired"),
            ("none", "free"),
        ],
    )
    async def test_denies_for_non_entitled_status(
        self, monkeypatch, status, expected_reason
    ):
        _install_profile(monkeypatch, _make_profile(status=status, tier="basic"))
        dep = require_feature(Feature.BASIC_ACCESS)
        with pytest.raises(HTTPException) as exc_info:
            await dep(_USER_CLAIMS)
        assert exc_info.value.status_code == 402
        assert exc_info.value.detail["reason"] == expected_reason

    @pytest.mark.asyncio
    async def test_denies_when_no_profile(self, monkeypatch):
        _install_profile(monkeypatch, None)
        dep = require_feature(Feature.BASIC_ACCESS)
        with pytest.raises(HTTPException) as exc_info:
            await dep(_USER_CLAIMS)
        assert exc_info.value.status_code == 402
        assert exc_info.value.detail["reason"] == "free"


# --------------------------------------------------------------------------- #
# Plus-only feature gates — denial path
# --------------------------------------------------------------------------- #


class TestPlusFeatureGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "feature",
        [
            Feature.PLUS_ACCESS,
            Feature.REFLECTION_ROOM_ACCESS,
            Feature.ECHO_SIGNATURE_ACCESS,
            Feature.MIRROR_MOMENT_ACCESS,
            Feature.CODE_LIBRARY_ACCESS,
        ],
    )
    async def test_basic_user_denied_with_feature_reason(self, monkeypatch, feature):
        """An active basic-tier user must NOT be able to reach a
        Plus-only route. The denial reason names the feature (not the
        status) so the client routes to the Plus upsell, not the
        re-subscribe flow."""
        _install_profile(monkeypatch, _make_profile(status="active", tier="basic"))
        dep = require_feature(feature)
        with pytest.raises(HTTPException) as exc_info:
            await dep(_USER_CLAIMS)
        assert exc_info.value.status_code == 402
        assert exc_info.value.detail["reason"] == feature.value
        assert exc_info.value.detail["code"] == "subscription_required"

    @pytest.mark.asyncio
    async def test_plus_user_passes(self, monkeypatch):
        _install_profile(monkeypatch, _make_profile(status="active", tier="plus"))
        dep = require_feature(Feature.PLUS_ACCESS)
        result = await dep(_USER_CLAIMS)
        assert result.profile.subscription_tier == "plus"

    @pytest.mark.asyncio
    async def test_plus_user_can_also_access_basic_routes(self, monkeypatch):
        """Plus is a superset of Basic — the launch tier_map encodes
        this."""
        _install_profile(monkeypatch, _make_profile(status="active", tier="plus"))
        dep = require_feature(Feature.BASIC_ACCESS)
        result = await dep(_USER_CLAIMS)
        assert result.profile.subscription_tier == "plus"


# --------------------------------------------------------------------------- #
# Status gate composes with tier gate — status wins on conflict
# --------------------------------------------------------------------------- #


class TestStatusGateOrdering:
    """When a user is in a non-entitled status, the 402 reason is
    derived from the status (trial_expired / expired / free) even if
    their tier would otherwise grant the requested feature. This lets
    the client route to the right paywall: re-subscribe, not upgrade.
    """

    @pytest.mark.asyncio
    async def test_plus_user_with_expired_status_gets_status_reason(self, monkeypatch):
        _install_profile(monkeypatch, _make_profile(status="expired", tier="plus"))
        dep = require_feature(Feature.PLUS_ACCESS)
        with pytest.raises(HTTPException) as exc_info:
            await dep(_USER_CLAIMS)
        assert exc_info.value.detail["reason"] == "expired"
        # Specifically NOT the feature-locked reason — tier alone
        # wouldn't deny here, but status does.
        assert exc_info.value.detail["reason"] != Feature.PLUS_ACCESS.value

    @pytest.mark.asyncio
    async def test_trial_user_with_expired_status_gets_trial_expired(self, monkeypatch):
        _install_profile(
            monkeypatch, _make_profile(status="trial_expired", tier="basic")
        )
        dep = require_feature(Feature.BASIC_ACCESS)
        with pytest.raises(HTTPException) as exc_info:
            await dep(_USER_CLAIMS)
        assert exc_info.value.detail["reason"] == "trial_expired"


# --------------------------------------------------------------------------- #
# require_entitled back-compat — old call sites still work
# --------------------------------------------------------------------------- #


class TestLegacyRequireEntitledAlias:
    """`require_entitled` predates the feature-flag refactor. Existing
    routes use `Depends(require_entitled)`; we don't want to touch them
    all at once. Pin the behaviour so the alias never silently drifts
    from `require_feature(BASIC_ACCESS)`.
    """

    @pytest.mark.asyncio
    async def test_alias_passes_for_basic_active_user(self, monkeypatch):
        _install_profile(monkeypatch, _make_profile(status="active", tier="basic"))
        result = await require_entitled(_USER_CLAIMS)
        assert isinstance(result, EntitledUser)

    @pytest.mark.asyncio
    async def test_alias_denies_for_expired_status(self, monkeypatch):
        _install_profile(monkeypatch, _make_profile(status="expired", tier="basic"))
        with pytest.raises(HTTPException) as exc_info:
            await require_entitled(_USER_CLAIMS)
        assert exc_info.value.status_code == 402
        assert exc_info.value.detail["reason"] == "expired"

    @pytest.mark.asyncio
    async def test_alias_denies_when_tier_is_free(self, monkeypatch):
        """Even if subscription_status is somehow 'active' but tier is
        'free' (data inconsistency or migration glitch), the alias
        still 402s because tier=free isn't in the BASIC_ACCESS set.
        Defence in depth."""
        _install_profile(monkeypatch, _make_profile(status="active", tier="free"))
        with pytest.raises(HTTPException) as exc_info:
            await require_entitled(_USER_CLAIMS)
        assert exc_info.value.status_code == 402
        assert exc_info.value.detail["reason"] == Feature.BASIC_ACCESS.value
