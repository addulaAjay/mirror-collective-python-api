"""
Entitlement enforcement — FastAPI dependency factory that gates paid
features.

Two-axis gate:
  1. Status — is the user's subscription currently live?
       status ∈ {trial, active, grace_period}  -> entitled
       status ∈ {none, trial_expired, expired, cancelled}  -> 402
  2. Tier  — does the user's `subscription_tier` grant the requested
     feature? (See `core.features.FEATURE_TIER_MAP`.)

Both checks must pass. The status gate is independent of the feature
so a Plus user whose card expired still gets a 402 (their tier grants
the feature but the entitlement isn't currently live). The tier gate
prevents a Basic user from reaching a Plus-only route even though
their status is fine.

Apple billing-retry is intentionally NOT entitled — we want the user
to update their payment method, and Apple's machinery handles the
retry.

Usage:

    from ..core.entitlement import require_feature, EntitledUser
    from ..core.features import Feature

    @router.post("/echoes")
    async def create_echo(
        req: CreateEchoRequest,
        entitled: EntitledUser = Depends(require_feature(Feature.BASIC_ACCESS)),
    ):
        user_id = entitled.user_id
        profile = entitled.profile        # UserProfile, already loaded
        ...

For backwards compatibility, the pre-feature-flag `require_entitled`
remains exported — it's now an alias for the BASIC_ACCESS gate. New
routes should prefer `require_feature(Feature.X)` so the intended
feature is named at the call site.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, NoReturn, Optional

from fastapi import Depends, HTTPException, status

from ..models.user_profile import UserProfile
from ..services.dynamodb_service import DynamoDBService
from .enhanced_auth import get_user_with_profile
from .features import FEATURE_TIER_MAP, Feature

logger = logging.getLogger(__name__)


ENTITLED_STATUSES = frozenset({"trial", "active", "grace_period"})
"""Subscription statuses that grant access to paid features.

Independent of which feature is being gated — every feature gate also
requires the user to be in one of these statuses.
"""


@dataclass(frozen=True)
class EntitledUser:
    """
    Bundle of identity + entitlement data returned by `require_feature`.

    `user` is the dict returned by `get_user_with_profile` (Cognito-enriched
    auth payload). `profile` is the DynamoDB UserProfile loaded inside the
    dependency. `user_id` is denormalised for convenience.
    """

    user_id: str
    user: Dict[str, Any]
    profile: UserProfile


def _lock_reason_from_status(status_value: str) -> str:
    """Map a non-entitled status to a UI-friendly reason code.

    Clients route to the appropriate paywall by inspecting
    detail['reason'].
    """
    if status_value == "trial_expired":
        return "trial_expired"
    if status_value in ("expired", "cancelled"):
        return "expired"
    # "none", "" or unknown values
    return "free"


# Backwards-compat alias — pre-feature-flag tests import `_lock_reason`.
_lock_reason = _lock_reason_from_status


def _raise_payment_required(reason: str, message: str) -> NoReturn:
    """
    Raise 402 Payment Required with a structured detail payload.
    """
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": "subscription_required",
            "reason": reason,
            "message": message,
        },
    )


def _status_message(reason: str) -> str:
    """User-facing copy for each status-based denial reason."""
    if reason == "trial_expired":
        return "Your trial has ended. Subscribe to keep using this feature."
    if reason == "expired":
        return "An active subscription is required for this feature."
    return "Start your free trial to use this feature."


def _feature_lock_message(feature: Feature) -> str:
    """User-facing copy when the user is in a live status but their
    tier doesn't grant the requested feature (e.g., Basic user hitting
    a Plus-only route).
    """
    if feature == Feature.PLUS_ACCESS:
        return "Mirror Plus is required for this feature."
    # Specific features: surface the feature name so the paywall can
    # render contextual copy. The wire reason already names it.
    return "This feature requires an upgraded subscription."


# Shared service instance — matches the pattern in subscription_routes.py
# (module-level DynamoDBService) and avoids a new boto3 client per request.
_dynamodb_service: Optional[DynamoDBService] = None


def _get_dynamodb_service() -> DynamoDBService:
    global _dynamodb_service
    if _dynamodb_service is None:
        _dynamodb_service = DynamoDBService()
    return _dynamodb_service


async def _load_and_check_status(
    current_user: Dict[str, Any],
) -> tuple[str, UserProfile]:
    """Shared prefix of every feature gate: load profile + run the
    status check. Returns `(user_id, profile)` when the user passes
    the status gate; raises 402 / 401 / 503 otherwise.

    Factored out so `require_feature` can layer the tier check on top
    without duplicating the profile-loading boilerplate.
    """
    user_id = current_user.get("id")
    if not user_id:
        # Should not happen — get_user_with_profile would have raised — but
        # be defensive so we never silently pass through to a 500.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing user id in claims",
        )

    dynamodb_service = _get_dynamodb_service()
    try:
        profile = await dynamodb_service.get_user_profile(user_id)
    except (
        Exception
    ) as exc:  # pragma: no cover — DynamoDB errors are rare and surface as 5xx
        logger.exception("Failed to load UserProfile for %s: %s", user_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not verify subscription. Please try again.",
        ) from exc

    if profile is None:
        # No profile row means the user signed up but has not started a
        # trial yet. Treat as "free" (not entitled).
        logger.info(
            "Entitlement check denied for %s — no UserProfile (free, pre-trial).",
            user_id,
        )
        _raise_payment_required(
            reason="free",
            message="Start your free trial to use this feature.",
        )

    status_value = (profile.subscription_status or "").lower()
    if status_value not in ENTITLED_STATUSES:
        reason = _lock_reason_from_status(status_value)
        logger.info(
            "Entitlement check denied for %s — status=%s reason=%s",
            user_id,
            status_value,
            reason,
        )
        _raise_payment_required(reason=reason, message=_status_message(reason))

    return user_id, profile


def require_feature(
    feature: Feature,
) -> Callable[[Dict[str, Any]], Awaitable[EntitledUser]]:
    """FastAPI dependency factory: gate a route on a specific feature.

    Returns an async dependency that:
      1. Loads the user's profile,
      2. Asserts the subscription status is in ENTITLED_STATUSES,
      3. Asserts the user's `subscription_tier` is in the tier set
         that grants `feature` (see FEATURE_TIER_MAP), and
      4. Returns an EntitledUser bundle.

    Failure modes (all 402 with structured `detail` payload):
      - reason="free" — no profile / status=none. Pre-trial.
      - reason="trial_expired" — trial ran out without conversion.
      - reason="expired" — paid subscription lapsed.
      - reason=<feature.value> — status is live but tier doesn't
        grant this feature (e.g., Basic user hitting a Plus route).
        Client routes to the upgrade paywall.

    Why a factory: FastAPI evaluates `Depends(...)` once per route at
    registration. Calling `require_feature(Feature.X)` returns the
    actual dependency callable; the closure captures `feature` so the
    dependency body knows which feature to check.
    """

    async def _dep(
        current_user: Dict[str, Any] = Depends(get_user_with_profile),
    ) -> EntitledUser:
        user_id, profile = await _load_and_check_status(current_user)

        tier = (profile.subscription_tier or "").lower()
        allowed_tiers = FEATURE_TIER_MAP[feature]
        if tier not in allowed_tiers:
            logger.info(
                "Feature gate denied for %s — tier=%s feature=%s allowed=%s",
                user_id,
                tier,
                feature.value,
                sorted(allowed_tiers),
            )
            _raise_payment_required(
                reason=feature.value,
                message=_feature_lock_message(feature),
            )

        return EntitledUser(user_id=user_id, user=current_user, profile=profile)

    return _dep


# ---------------------------------------------------------------------------
# Backwards-compatibility shim.
#
# Every existing route uses `Depends(require_entitled)` from before the
# feature-flag refactor. That call signature was a bare async function,
# not a factory — re-implementing it via `require_feature(BASIC_ACCESS)`
# would require touching every call site.
#
# Instead, keep `require_entitled` as a plain async function that runs
# the BASIC_ACCESS gate. New routes should use
# `Depends(require_feature(Feature.X))` so the gated feature is named.
# ---------------------------------------------------------------------------


async def require_entitled(
    current_user: Dict[str, Any] = Depends(get_user_with_profile),
) -> EntitledUser:
    """Backwards-compatible alias for `require_feature(Feature.BASIC_ACCESS)`.

    Kept so the dozens of existing `Depends(require_entitled)` call
    sites don't all need editing. New code should prefer
    `require_feature(Feature.X)` to name its intent.

    Delegates to the factory inner dependency so the gate logic
    lives in exactly one place — a future change to BASIC_ACCESS
    semantics (e.g., adding a kill-switch) lands in
    `require_feature` and this alias picks it up automatically.
    """
    inner = require_feature(Feature.BASIC_ACCESS)
    return await inner(current_user)
