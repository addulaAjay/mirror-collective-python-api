"""
Enhanced user dependencies for profile data.

Wave 1B perf change — skip Cognito GetUser on the hot path
==========================================================
The original implementation called ``cognito_service.get_user(access_token)``
on every request behind ``get_user_with_profile``. AWS Cognito's GetUser is
account-wide capped at ~120 RPS, which becomes the single largest quota
cliff in the system once the app sees 1k+ concurrent users.

The fix has two layers:

1. **Claim short-circuit.** ``get_current_user`` (in ``core/security.py``)
   already maps the JWT claims into a profile dict. When the caller sends
   an **ID token**, the claims include ``email``, ``given_name``,
   ``family_name`` and ``email_verified`` — everything we need for the
   "enhanced" profile. In that case we synthesize ``enhanced_user``
   directly from ``current_user`` and skip Cognito entirely.

2. **In-process TTL cache.** When the caller sends an access token only
   (no ``email``/``firstName``/``lastName`` claims), we still need
   GetUser. We cache the result keyed by Cognito ``sub`` for
   ``COGNITO_PROFILE_CACHE_TTL_SECONDS`` (default 300s) so subsequent
   requests from the same warm Lambda container reuse the resolved
   profile. The cache is module-level state guarded by a ``Lock`` and
   capped via an ``OrderedDict`` (LRU eviction).

If both paths fail we still return ``_create_fallback_user`` so
endpoints depending on this never 500 because of a profile lookup.
"""

import logging
import os
import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Dict, Optional, Tuple

from fastapi import Depends, Request

from ..services.cognito_service import CognitoService, get_cognito_service
from .security import get_current_user

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# In-process profile cache
# --------------------------------------------------------------------------- #
# Maps Cognito sub -> (expires_at_epoch_seconds, profile_dict).
# Module-level state is intentional: a warm Lambda container reuses this
# between invocations, which is exactly what we want for cutting GetUser RPS.
# The cache is keyed strictly by sub so it cannot leak across user
# identities.
_PROFILE_CACHE: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()
_PROFILE_CACHE_LOCK = Lock()
_PROFILE_CACHE_MAX_ENTRIES = 10_000


def _cache_ttl_seconds() -> int:
    """TTL for the in-process profile cache, configurable via env var."""
    raw = os.getenv("COGNITO_PROFILE_CACHE_TTL_SECONDS", "300")
    try:
        ttl = int(raw)
    except ValueError:
        logger.warning(
            "Invalid COGNITO_PROFILE_CACHE_TTL_SECONDS=%r, using default 300",
            raw,
        )
        return 300
    return ttl if ttl > 0 else 300


def _evict_expired(now: float) -> None:
    """Drop any entries whose TTL has elapsed. Caller must hold the lock."""
    expired_keys = [
        key for key, (expires_at, _) in _PROFILE_CACHE.items() if expires_at <= now
    ]
    for key in expired_keys:
        _PROFILE_CACHE.pop(key, None)


def _cache_get(sub: str) -> Optional[Dict[str, Any]]:
    """Return a cached profile for sub if still fresh, else None."""
    if not sub:
        return None
    now = time.monotonic()
    with _PROFILE_CACHE_LOCK:
        entry = _PROFILE_CACHE.get(sub)
        if entry is None:
            return None
        expires_at, profile = entry
        if expires_at <= now:
            _PROFILE_CACHE.pop(sub, None)
            return None
        # LRU bump
        _PROFILE_CACHE.move_to_end(sub)
        return profile


def _cache_set(sub: str, profile: Dict[str, Any]) -> None:
    """Store a profile for sub with the configured TTL."""
    if not sub:
        return
    ttl = _cache_ttl_seconds()
    now = time.monotonic()
    expires_at = now + ttl
    with _PROFILE_CACHE_LOCK:
        _evict_expired(now)
        _PROFILE_CACHE[sub] = (expires_at, profile)
        _PROFILE_CACHE.move_to_end(sub)
        # Cap size with LRU eviction
        while len(_PROFILE_CACHE) > _PROFILE_CACHE_MAX_ENTRIES:
            _PROFILE_CACHE.popitem(last=False)


def _reset_cache_for_tests() -> None:
    """Test-only helper: clear the in-process cache between scenarios."""
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE.clear()


# --------------------------------------------------------------------------- #
# Profile assembly helpers
# --------------------------------------------------------------------------- #
def _build_display_name(
    given_name: str,
    family_name: str,
    email: str,
    user_id: str,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """Mirror the legacy display-name logic.

    Order of preference:
      1. "given_name family_name"
      2. attrs["name"]
      3. attrs["preferred_username"]
      4. email prefix (before "@")
      5. "User-<first 8 chars of sub>"
    """
    display_name = f"{given_name} {family_name}".strip()
    if not display_name and extra:
        display_name = extra.get("name") or ""
    if not display_name and extra:
        display_name = extra.get("preferred_username") or ""
    if not display_name and email:
        display_name = email.split("@")[0]
    if not display_name:
        display_name = f"User-{(user_id or '')[:8]}"
    return display_name


def _profile_from_jwt_claims(current_user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return enhanced_user synthesized from JWT claims, or None if missing.

    Returns None when the JWT did not carry enough profile info — typically
    because the caller sent an access token (which Cognito does not put
    name/email claims on). In that case the caller must fall back to
    Cognito GetUser (with caching) before giving up.
    """
    email = current_user.get("email") or ""
    first_name = current_user.get("firstName") or ""
    last_name = current_user.get("lastName") or ""

    # We need at least email AND (first_name OR last_name) to consider the
    # JWT "enriched enough" to skip Cognito. Email alone is not enough
    # because some legacy paths populate it from elsewhere; we want the
    # name fields too to truly avoid the GetUser dependency.
    if not email or not (first_name or last_name):
        return None

    user_id = current_user.get("id") or current_user.get("sub") or ""
    display_name = _build_display_name(first_name, last_name, email, user_id)

    return {
        **current_user,
        "email": email,
        "firstName": first_name,
        "lastName": last_name,
        "name": display_name,
        "emailVerified": bool(current_user.get("emailVerified", False)),
        "cognitoUsername": current_user.get("cognitoUsername", ""),
        "userStatus": current_user.get("userStatus", "UNKNOWN"),
    }


def _profile_from_cognito(
    current_user: Dict[str, Any], cognito_user: Dict[str, Any]
) -> Dict[str, Any]:
    """Build enhanced_user dict from a Cognito GetUser response."""
    attrs: Dict[str, Any] = cognito_user.get("userAttributes") or {}
    given_name = attrs.get("given_name", "") or ""
    family_name = attrs.get("family_name", "") or ""
    email = attrs.get("email") or current_user.get("email") or ""
    user_id = current_user.get("id") or current_user.get("sub") or ""

    display_name = _build_display_name(
        given_name, family_name, email, user_id, extra=attrs
    )

    return {
        **current_user,
        "email": email,
        "firstName": given_name,
        "lastName": family_name,
        "name": display_name,
        "emailVerified": str(attrs.get("email_verified", "false")).lower() == "true",
        "cognitoUsername": cognito_user.get("username", ""),
        "userStatus": cognito_user.get("userStatus", "UNKNOWN"),
    }


# --------------------------------------------------------------------------- #
# FastAPI dependency
# --------------------------------------------------------------------------- #
async def get_user_with_profile(
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
    cognito_service: CognitoService = Depends(get_cognito_service),
) -> Dict[str, Any]:
    """Return a user profile enriched with name/email/verified flags.

    Resolution order:
      1. If the JWT already carried email + given_name/family_name claims
         (ID token path), build the enhanced user directly from those
         claims — **no Cognito call**.
      2. Else, consult the in-process TTL cache keyed by sub. If we have
         a fresh entry, use it — **no Cognito call**.
      3. Else, call ``cognito_service.get_user(access_token)`` and cache
         the resulting profile.
      4. If all of the above fail, return ``_create_fallback_user``.
    """
    # Step 1: try to satisfy from JWT claims alone.
    jwt_profile = _profile_from_jwt_claims(current_user)
    if jwt_profile is not None:
        logger.debug(
            "enhanced_auth: served profile from JWT claims (no Cognito call) "
            "for sub=%s",
            current_user.get("id"),
        )
        return jwt_profile

    sub = current_user.get("id") or current_user.get("sub") or ""

    # Step 2: try the cache.
    cached = _cache_get(sub) if sub else None
    if cached is not None:
        logger.debug(
            "enhanced_auth: served profile from cache (no Cognito call) for sub=%s",
            sub,
        )
        return cached

    # Step 3: fall back to Cognito GetUser with the bearer access token.
    try:
        access_token = _extract_bearer_token(request)
        if not access_token:
            logger.warning("No access token found in request for profile fetch")
            return _create_fallback_user(current_user)

        cognito_user = await cognito_service.get_user(access_token)
        if cognito_user and cognito_user.get("userAttributes"):
            enhanced_user = _profile_from_cognito(current_user, cognito_user)
            if sub:
                _cache_set(sub, enhanced_user)
            logger.info(
                "Enhanced user profile created for %s",
                enhanced_user.get("email") or current_user.get("id"),
            )
            return enhanced_user

    except Exception as e:  # noqa: BLE001 — log + fall through to fallback
        logger.warning(
            "Failed to fetch Cognito profile for user %s: %s",
            current_user.get("id"),
            str(e),
        )

    # Step 4: final fallback (never raises).
    return _create_fallback_user(current_user)


def _extract_bearer_token(request: Request) -> Optional[str]:
    """Pull the bearer token out of the Authorization header, if any."""
    auth_header = request.headers.get("authorization") or request.headers.get(
        "Authorization"
    )
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1]
    return None


def _create_fallback_user(current_user: Dict[str, Any]) -> Dict[str, Any]:
    """Create fallback user profile when Cognito data is unavailable."""
    email = current_user.get("email", "") or ""
    user_id = current_user.get("id") or current_user.get("sub") or ""
    fallback_name = email.split("@")[0] if email else f"User-{user_id[:8]}"

    return {
        **current_user,
        "name": fallback_name,
        "firstName": "",
        "lastName": "",
        "emailVerified": False,
        "cognitoUsername": "",
        "userStatus": "UNKNOWN",
    }
