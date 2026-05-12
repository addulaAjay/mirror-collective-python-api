"""
Entitlement enforcement — FastAPI dependency that gates paid features.

Entitlement matrix (locked 2026-05-11):

  status in {trial, active, grace_period}  -> entitled
  status in {none, trial_expired, expired, cancelled}  -> 402 Payment Required

`tier` only differentiates the storage quota; gates use status alone.

Apple billing-retry is intentionally NOT entitled — we want the user to
update their payment method, and Apple's machinery handles the retry.

The dependency loads the UserProfile from DynamoDB once and returns it
along with the auth user dict, so handlers don't need to re-fetch the
profile inside their bodies.

Usage:

    from ..core.entitlement import require_entitled, EntitledUser

    @router.post("/echoes")
    async def create_echo(
        req: CreateEchoRequest,
        entitled: EntitledUser = Depends(require_entitled),
    ):
        user_id = entitled.user_id
        profile = entitled.profile        # UserProfile, already loaded
        ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, NoReturn, Optional

from fastapi import Depends, HTTPException, status

from ..models.user_profile import UserProfile
from ..services.dynamodb_service import DynamoDBService
from .enhanced_auth import get_user_with_profile

logger = logging.getLogger(__name__)


ENTITLED_STATUSES = frozenset({"trial", "active", "grace_period"})
"""Subscription statuses that grant access to paid features."""


@dataclass(frozen=True)
class EntitledUser:
    """
    Bundle of identity + entitlement data returned by `require_entitled`.

    `user` is the dict returned by `get_user_with_profile` (Cognito-enriched
    auth payload). `profile` is the DynamoDB UserProfile loaded inside the
    dependency. `user_id` is denormalised for convenience.
    """

    user_id: str
    user: Dict[str, Any]
    profile: UserProfile


def _lock_reason(status_value: str) -> str:
    """Map a non-entitled status to a UI-friendly reason code."""
    if status_value == "trial_expired":
        return "trial_expired"
    if status_value in ("expired", "cancelled"):
        return "expired"
    # "none", "" or unknown values
    return "free"


def _raise_payment_required(reason: str, message: str) -> NoReturn:
    """
    Raise 402 Payment Required with a structured detail payload.

    Clients can route to the appropriate paywall by inspecting
    detail['reason'].
    """
    raise HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "code": "subscription_required",
            "reason": reason,
            "message": message,
        },
    )


# Shared service instance — matches the pattern in subscription_routes.py
# (module-level DynamoDBService) and avoids a new boto3 client per request.
_dynamodb_service: Optional[DynamoDBService] = None


def _get_dynamodb_service() -> DynamoDBService:
    global _dynamodb_service
    if _dynamodb_service is None:
        _dynamodb_service = DynamoDBService()
    return _dynamodb_service


async def require_entitled(
    current_user: Dict[str, Any] = Depends(get_user_with_profile),
) -> EntitledUser:
    """
    FastAPI dependency: require an entitled subscription state.

    Raises 402 Payment Required when:
      - the user has no DynamoDB profile yet (brand-new signup pre-trial), or
      - subscription_status is not in {trial, active, grace_period}.

    Returns an EntitledUser bundle the handler can reuse.
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
        reason = _lock_reason(status_value)
        logger.info(
            "Entitlement check denied for %s — status=%s reason=%s",
            user_id,
            status_value,
            reason,
        )
        _raise_payment_required(
            reason=reason,
            message=(
                "Your trial has ended. Subscribe to keep using this feature."
                if reason == "trial_expired"
                else (
                    "An active subscription is required for this feature."
                    if reason == "expired"
                    else "Start your free trial to use this feature."
                )
            ),
        )

    return EntitledUser(user_id=user_id, user=current_user, profile=profile)
