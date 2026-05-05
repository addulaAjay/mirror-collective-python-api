"""Session lifetime + timezone helpers for Reflection Room (spec §3.1, §6.1, §8.3).

A session expires at the next midnight in the user's IANA timezone, captured at
session creation. ``expires_at`` is **not** slid forward on subsequent quiz
submissions within the same session — mid-day midnight crossings are explicitly
out-of-scope per spec §8.3.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...models.reflection_session import ReflectionSession

logger = logging.getLogger(__name__)

DEFAULT_TZ_FALLBACK = "America/New_York"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Render a UTC datetime as ISO 8601 with ``Z`` suffix."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 string, tolerating the ``Z`` suffix."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def resolve_user_tz(header_tz: Optional[str]) -> str:
    """Tz resolution order (V1 reduced, see spec §6.1):
      1. ``X-User-Timezone`` header if present and valid IANA name
      2. Default from env ``REFLECTION_DEFAULT_USER_TZ`` (or hard fallback)

    The user-record fallback (spec step 2) is deferred — UserProfile has no tz
    field yet. Track in V2.
    """
    if header_tz:
        try:
            ZoneInfo(header_tz)
            return header_tz
        except ZoneInfoNotFoundError:
            logger.warning(f"Invalid X-User-Timezone header value: {header_tz!r}")
    return os.getenv("REFLECTION_DEFAULT_USER_TZ", DEFAULT_TZ_FALLBACK)


def next_midnight_in_tz(now: datetime, tz_name: str) -> datetime:
    """Return the next 00:00:00.000 wall-clock in ``tz_name`` as a UTC datetime.

    ``now`` should be UTC-aware; if naive, it's assumed UTC.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    next_local = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return next_local.astimezone(timezone.utc)


def compute_session_window(
    user_tz: str, now: Optional[datetime] = None
) -> tuple[str, str, int]:
    """Return ``(created_at_iso, expires_at_iso, ttl_epoch_30d)`` for a new session.

    ``ttl_epoch_30d`` is for DDB TTL storage cleanup only; app-level "active"
    checks use ``expires_at`` (per spec §3.1 note).
    """
    n = now or now_utc()
    expires_at = next_midnight_in_tz(n, user_tz)
    ttl_epoch = int((n + timedelta(days=30)).timestamp())
    return iso(n), iso(expires_at), ttl_epoch


def is_active(session: ReflectionSession, now: Optional[datetime] = None) -> bool:
    """True if the session has not yet expired in its own timezone."""
    if not session or not session.expires_at:
        return False
    n = now or now_utc()
    try:
        return n < parse_iso(session.expires_at)
    except ValueError:
        logger.warning(f"Could not parse session.expires_at={session.expires_at!r}")
        return False
