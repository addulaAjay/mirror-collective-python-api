"""Personalization scoring (spec §9.2).

Score each candidate practice by combining:

  * Helpfulness votes with 21-day half-life decay.
  * +0.5 boost if the user's current time-of-day bucket matches their
    most-completed bucket.
  * -1.0 penalty if the user used the practice within the last 24h.

Pure function — DDB I/O happens upstream in the recommender.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...models.user_personalization import UserPersonalization
from .catalog_loader import Practice
from .personalization_loader import PersonalizationDefaults

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoredPractice:
    practice: Practice
    score: float


def score(
    candidates: Sequence[Practice],
    prefs: UserPersonalization,
    defaults: PersonalizationDefaults,
    *,
    user_tz: str,
    now: Optional[datetime] = None,
) -> List[ScoredPractice]:
    """Return scored candidates in the same order as input."""
    n = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    bucket_now = _bucket_for(n, user_tz, defaults.time_of_day_buckets)
    most_common_bucket = _most_common_bucket(prefs.time_of_day_history)

    out: List[ScoredPractice] = []
    for p in candidates:
        s = 0.0
        s += _helpfulness_score(p.id, prefs, defaults, n)
        s += _time_of_day_score(bucket_now, most_common_bucket, prefs, defaults)
        s += _recent_use_penalty(p.id, prefs, defaults, n)
        out.append(ScoredPractice(practice=p, score=round(s, 4)))
    return out


# ============================================================
# Component scores
# ============================================================


def _helpfulness_score(
    practice_id: str,
    prefs: UserPersonalization,
    defaults: PersonalizationDefaults,
    now: datetime,
) -> float:
    history = prefs.practice_helpfulness.get(practice_id, [])
    if not history:
        return 0.0
    half_life_days = float(defaults.decay.recency_decay_half_life_days)
    if half_life_days <= 0:
        # Defensive: if half-life is zero, treat all events as fully decayed.
        return 0.0

    total = 0.0
    for ev in history:
        ev_dt = _parse_iso(ev.ts)
        if ev_dt is None:
            continue
        age_days = max(0.0, (now - ev_dt).total_seconds() / 86400.0)
        decay = 0.5 ** (age_days / half_life_days)
        if ev.helpful:
            total += defaults.weights.helpful_vote * decay
        else:
            total += defaults.weights.not_helpful_vote * decay
    return total


def _time_of_day_score(
    bucket_now: str,
    most_common: Optional[str],
    prefs: UserPersonalization,
    defaults: PersonalizationDefaults,
) -> float:
    """Spec §9.2: boost only when the user has at least one completion in the
    current bucket AND that bucket is their most-common one."""
    if not most_common or most_common != bucket_now:
        return 0.0
    if prefs.time_of_day_history.get(bucket_now, 0) <= 0:
        return 0.0
    return defaults.weights.time_of_day_match


def _recent_use_penalty(
    practice_id: str,
    prefs: UserPersonalization,
    defaults: PersonalizationDefaults,
    now: datetime,
) -> float:
    entry = prefs.recent_use.get(practice_id)
    if entry is None:
        return 0.0
    last_used = _parse_iso(entry.last_used_at)
    if last_used is None:
        return 0.0
    if (now - last_used).total_seconds() < 24 * 3600:
        return defaults.weights.recent_use_penalty
    return 0.0


# ============================================================
# Bucket helpers
# ============================================================


def bucket_for_now(
    now_utc: datetime, user_tz: str, buckets: Dict[str, List[int]]
) -> str:
    """Public alias of :func:`_bucket_for` for callers outside the personalizer
    (e.g. ``POST /practice/complete`` needs the bucket name for
    ``time_of_day_history``)."""
    return _bucket_for(now_utc, user_tz, buckets)


def _bucket_for(now_utc: datetime, user_tz: str, buckets: Dict[str, List[int]]) -> str:
    """Return the bucket name that contains ``now_utc`` in ``user_tz`` local
    time. Wraparound buckets like ``[21, 5]`` are supported.

    Falls back to ``"midday"`` if the buckets are misconfigured.
    """
    try:
        tz = ZoneInfo(user_tz)
    except ZoneInfoNotFoundError:
        logger.warning(f"unknown user_tz {user_tz!r}; using UTC")
        tz = ZoneInfo("UTC")
    local_hour = now_utc.astimezone(tz).hour
    for name, range_pair in buckets.items():
        if not (isinstance(range_pair, list) and len(range_pair) == 2):
            continue
        start, end = int(range_pair[0]), int(range_pair[1])
        if start <= end:
            if start <= local_hour < end:
                return name
        else:
            # Wraparound (e.g. night: [21, 5]).
            if local_hour >= start or local_hour < end:
                return name
    return "midday"


def _most_common_bucket(history: Dict[str, int]) -> Optional[str]:
    if not history:
        return None
    return max(history.items(), key=lambda kv: kv[1])[0]


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None
