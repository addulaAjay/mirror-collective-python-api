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

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

from fastapi import Depends, HTTPException, Request

from ..services.dynamodb_service import get_dynamodb_service
from ..services.storage_quota_service import get_storage_quota_service
from .security import get_current_user

# Tiers that grant Echo Vault (Basic) access. Mirrors GET /api/subscriptions/
# status, where features.echo_vault_enabled == (tier in these). "core"/
# "core_plus" are the internal tier ids; the product is displayed as "Mirror
# Basic".
_ENTITLED_TIERS = frozenset({"trial", "core", "core_plus"})

# Paid tiers whose access must also be gated on the subscription's expiry (not
# just the tier). "trial" is handled separately via trial_expires_at.
_PAID_TIERS = frozenset({"core", "core_plus"})

_SUBSCRIPTIONS_TABLE = os.getenv("DYNAMODB_SUBSCRIPTIONS_TABLE", "subscriptions")

# Non-mutating methods stay open so users keep read access to an existing vault.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# upload_type values that are account-level (the user's own avatar), NOT Echo
# Vault content. These don't require an entitlement — an expired user must still
# be able to change their profile picture. Mirrors the quota exemption in
# get_upload_url, which already only counts "echo" uploads.
_ACCOUNT_UPLOAD_TYPES = frozenset({"profile", "user_profile"})

# The one route whose entitlement need depends on the request body (upload_type)
# rather than the method alone.
_UPLOAD_URL_SUFFIX = "/echoes/upload-url"

SUBSCRIPTION_REQUIRED_MESSAGE = (
    "An active trial or subscription is required to use Echo Vault."
)

# ── TEMPORARY client-crash mitigation ────────────────────────────────────────
#
# Shipped app versions (<= 1.0.1) crash when this 403 carries the
# machine-readable ``code``. Their BaseApiService matches on it and emits a
# `subscriptionRequired` event, which navigates to the paywall — while the
# calling screen ALSO fires its own `Alert.alert`. Presenting an alert from a
# screen that is mid-transition is a hard crash on iOS, and it is
# deterministic: every unentitled user who tries to add a recipient loses the
# app. Observed in production 2026-10-11.
#
# Dropping the code to a plain string downgrades that to a readable error
# message ("An active trial or subscription is required...") with no paywall
# hop. Worse UX than intended, far better than a crash — and it needs no App
# Store release.
#
# REVERT once a client carrying the fix (don't alert when the paywall event
# already fired) is the minimum supported version: set
# ECHO_VAULT_EMIT_SUBSCRIPTION_CODE=true. That is an env change, not a deploy.
EMIT_SUBSCRIPTION_CODE = (
    os.getenv("ECHO_VAULT_EMIT_SUBSCRIPTION_CODE", "false").lower() == "true"
)

# First client version that survives receiving the code — i.e. the first build
# carrying the fix that suppresses its own alert while the paywall redirect is
# in flight. Clients at or above this may receive the machine-readable code;
# everything else gets the plain string.
MIN_CLIENT_FOR_SUBSCRIPTION_CODE = os.getenv(
    "ECHO_VAULT_MIN_CLIENT_FOR_SUBSCRIPTION_CODE", "1.1.0"
)

# Clients report themselves here. Absent on every build shipped before
# 2026-11-01, which is precisely the population that must not get the code.
APP_VERSION_HEADER = "x-app-version"


def _parse_version(raw: Optional[str]) -> Optional[tuple]:
    """``"1.1.0 (34)"`` -> ``(1, 1, 0)``. ``None`` when it can't be trusted.

    Numeric tuple, not string compare: "1.10.0" must sort above "1.9.0".
    The header is attacker-controlled, so anything unexpected yields None and
    the caller fails closed.
    """
    if not raw:
        return None
    # Take the leading dotted-numeric run; ignore a build suffix like " (34)".
    match = re.match(r"\s*(\d+(?:\.\d+)*)", raw)
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except (ValueError, TypeError):  # pragma: no cover - re guarantees digits
        return None


def _client_can_handle_subscription_code(request: Any) -> bool:
    """Does this caller identify as a build that survives the code?

    Fails closed on every ambiguity — no request, no header, unparseable
    header. A false negative costs a worse error message; a false positive
    costs a hard crash on the user's device.
    """
    if request is None:
        return False
    try:
        raw = request.headers.get(APP_VERSION_HEADER)
    except Exception:  # noqa: BLE001 - any odd request object fails closed
        return False

    client = _parse_version(raw)
    minimum = _parse_version(MIN_CLIENT_FOR_SUBSCRIPTION_CODE)
    if client is None or minimum is None:
        return False
    return client >= minimum


def subscription_required_detail(request: Any = None) -> Union[str, Dict[str, str]]:
    """Body for the entitlement 403.

    The machine-readable code triggers a paywall redirect on the client. It is
    only safe for builds that suppress their own error alert while that
    redirect runs — older clients present an alert mid-transition and crash
    (production, 2026-10-11). So it requires BOTH:

      * ``ECHO_VAULT_EMIT_SUBSCRIPTION_CODE=true`` (master kill switch), and
      * an ``X-App-Version`` at or above ``MIN_CLIENT_FOR_SUBSCRIPTION_CODE``.

    Everything else gets the plain string, which is a worse experience than a
    paywall hop and a far better one than a crash.

    Both values are read at raise time rather than bound into the decision at
    import, so a Lambda env change takes effect on the next cold start without
    a code deploy.
    """
    if EMIT_SUBSCRIPTION_CODE and _client_can_handle_subscription_code(request):
        return {
            "code": "subscription_required",
            "message": SUBSCRIPTION_REQUIRED_MESSAGE,
        }
    return SUBSCRIPTION_REQUIRED_MESSAGE


_dynamodb = get_dynamodb_service()
_quota = get_storage_quota_service()


def _trial_has_lapsed(expires_at: Optional[str]) -> bool:
    """True when a trial's ``trial_expires_at`` is in the past.

    The daily expiry cron only flips ``subscription_tier`` to ``free`` once a
    day, so a trial that lapses between runs still reads ``tier == "trial"``.
    Checking the timestamp here restricts the user the moment the trial ends
    rather than up to ~24h later, closing that accuracy gap. A missing or
    unparseable timestamp is treated as lapsed — fail closed.
    """
    if not expires_at:
        return True
    try:
        end = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return True
    return end <= datetime.now(timezone.utc)


async def _paid_subscription_lapsed(profile: Any) -> bool:
    """True only when a paid subscription can be PROVEN expired.

    Loads the user's primary subscription and returns True iff it carries an
    ``expiry_date`` in the past. A missing record, an absent expiry, or any read
    error returns False — we fail OPEN so a real subscriber is never wrongly
    locked out over a transient read or an unpopulated field. The Apple
    ``EXPIRED``/``REFUND`` webhook (and a reconciliation job) remain the
    authoritative downgrade; this is a read-time safety net so a single missed
    or reordered webhook can't grant indefinite access after a lapse.
    """
    sub_id = getattr(profile, "primary_subscription_id", None)
    user_id = getattr(profile, "user_id", None)
    if not sub_id or not user_id:
        return False
    try:
        item = await _dynamodb.get_item(
            _SUBSCRIPTIONS_TABLE,
            {"user_id": user_id, "subscription_id": sub_id},
        )
    except Exception:  # noqa: BLE001 — read failure must not lock out a user
        return False
    if not item:
        return False
    # A record that still reads active AND is set to auto-renew is not a proven
    # lapse — a past expiry_date on it means the stored field is stale (a missed/
    # reordered renewal or a single-transaction lookup that reported the original
    # period), not that the user stopped paying. The EXPIRED/REFUND webhook flips
    # status when the subscription genuinely ends, and that's what should gate.
    if item.get("status") == "active" and item.get("auto_renew_enabled"):
        return False
    expiry = item.get("expiry_date")
    if not expiry:
        return False
    try:
        end = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return end <= datetime.now(timezone.utc)


def _profile_is_entitled(profile: Any) -> bool:
    """True when a user profile grants active Echo Vault access.

    Entitled = tier in {trial, core, core_plus}, and — for a trial — still
    within its window (paid tiers ignore the trial timestamp, governed instead
    by the subscription lifecycle).
    """
    tier = getattr(profile, "subscription_tier", None) if profile else None
    if tier not in _ENTITLED_TIERS:
        return False
    if tier == "trial" and _trial_has_lapsed(
        getattr(profile, "trial_expires_at", None)
    ):
        return False
    return True


async def _is_account_level_upload(request: Request) -> bool:
    """True when this is the upload-url route asking for an account-level
    (profile/avatar) upload, which needs no Echo Vault entitlement.

    Peeks at the JSON body; Starlette caches it on the request, so the route
    handler still parses ``UploadUrlRequest`` normally afterwards. Any parse
    failure falls through to ``False`` (treat as gated) — fail closed.
    """
    if not request.url.path.endswith(_UPLOAD_URL_SUFFIX):
        return False
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed body → let the route 4xx it
        return False
    return (body.get("upload_type") or "echo") in _ACCOUNT_UPLOAD_TYPES


async def require_echo_vault_access(
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """Router dependency: gate Echo Vault mutations behind an active entitlement.

    Raises 403 ``{"code": "subscription_required"}`` when the caller's tier is
    not entitled (free / trial-expired / never subscribed) or when a trial tier
    has passed its expiry. GET/HEAD/OPTIONS pass through so existing content
    stays viewable, and account-level avatar uploads pass through so an expired
    user can still manage their own profile picture.
    """
    if request.method in _READ_METHODS:
        return current_user

    if await _is_account_level_upload(request):
        return current_user

    profile = await _dynamodb.get_user_profile(current_user["id"])
    if not _profile_is_entitled(profile):
        raise HTTPException(
            status_code=403, detail=subscription_required_detail(request)
        )

    # Paid tiers were entitled purely by tier — a lapsed/refunded paid user kept
    # access whenever the EXPIRED webhook was missed/delayed/reordered. Also gate
    # on a proven-expired subscription (fail open on any doubt). This likewise
    # catches a StoreKit trial whose window ended without converting (tier stays
    # "core", so the trial_expires_at branch above doesn't apply).
    tier = getattr(profile, "subscription_tier", None) if profile else None
    if tier in _PAID_TIERS and await _paid_subscription_lapsed(profile):
        raise HTTPException(
            status_code=403, detail=subscription_required_detail(request)
        )

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
