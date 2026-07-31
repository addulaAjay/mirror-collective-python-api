"""Entitlement guard for the Basic subscription tier (Echo Vault).

Server-side enforcement is authoritative — the client also gates for UX, but
that's bypassable. Reads stay OPEN so an expired user can still see their
existing vault; mutations (create / upload / modify) require an active
entitlement (trial or a paid subscription), and uploads additionally respect
the storage quota.

The 403 body carries a machine-readable ``code: "subscription_required"`` so the
client can navigate to the paywall instead of showing a generic error. Over-
quota uploads return 507 with ``code`` ``"quota_exceeded"`` / ``"no_quota"``.
"""

from typing import Any, Dict

from fastapi import Depends, HTTPException, Request

from ..services.dynamodb_service import get_dynamodb_service
from ..services.storage_quota_service import get_storage_quota_service
from .security import get_current_user

# Tiers that grant Echo Vault (Basic) access. Mirrors GET /api/subscriptions/
# status, where features.echo_vault_enabled == (tier in these). "core"/
# "core_plus" are the internal tier ids; the product is displayed as "Mirror
# Basic".
_ENTITLED_TIERS = frozenset({"trial", "core", "core_plus"})

# Non-mutating methods stay open so users keep read access to an existing vault.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

SUBSCRIPTION_REQUIRED_DETAIL: Dict[str, str] = {
    "code": "subscription_required",
    "message": "An active trial or subscription is required to use Echo Vault.",
}

_dynamodb = get_dynamodb_service()
_quota = get_storage_quota_service()


async def require_echo_vault_access(
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Router dependency: gate Echo Vault mutations behind an active entitlement.

    Raises 403 ``{"code": "subscription_required"}`` when the caller's tier is
    not entitled (free / trial-expired / never subscribed). GET/HEAD/OPTIONS
    pass through so existing content stays viewable.
    """
    if request.method in _READ_METHODS:
        return current_user

    profile = await _dynamodb.get_user_profile(current_user["id"])
    tier = getattr(profile, "subscription_tier", None) if profile else None
    if tier not in _ENTITLED_TIERS:
        raise HTTPException(status_code=403, detail=SUBSCRIPTION_REQUIRED_DETAIL)
    return current_user


async def enforce_upload_quota(user_id: str, file_size_bytes: int = 0) -> None:
    """Reject an upload when the user is at/over their storage quota.

    ``file_size_bytes`` is best-effort — the upload-grant endpoints don't carry
    the size yet, so a 0 still blocks a user who is already over quota (or has
    none). Raises 507 (Insufficient Storage) with a machine-readable code.
    """
    result = await _quota.can_upload(user_id, file_size_bytes)
    if not result.get("can_upload", False):
        raise HTTPException(
            status_code=507,  # Insufficient Storage
            detail={
                "code": result.get("reason", "quota_exceeded"),
                "message": result.get("message", "Storage quota exceeded."),
            },
        )
