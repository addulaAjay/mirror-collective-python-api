"""Rule matcher (spec §8.4).

Pure function: given a single ``LoopState`` (or shape-equivalent object) and a
list of ``PracticeRule``, return the rules whose ``when`` conditions hold.

V1 gates on ``loop_id`` only (spec §8.4) — ``motif_any`` and
``narrative_stage_in`` clauses on the rule schema are reserved for V2 and
ignored here. ``recent_days_max`` is enforced via UTC comparison (a few hours
of timezone offset doesn't materially affect a 3-day window).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from .rule_loader import PracticeRule


def match(
    loop,
    rules: Sequence[PracticeRule],
    *,
    now: Optional[datetime] = None,
) -> List[PracticeRule]:
    """Return all rules whose ``when`` clause matches the given loop."""
    n = now or datetime.now(timezone.utc)
    return [r for r in rules if _rule_matches(loop, r, n)]


def _rule_matches(loop, rule: PracticeRule, now: datetime) -> bool:
    when = rule.when
    if loop.loop_id != when.loop_id:
        return False
    if when.min_strength is not None and float(loop.intensity_score) < float(
        when.min_strength
    ):
        return False
    if when.trend_in and loop.tone_state not in when.trend_in:
        return False
    if when.recent_days_max is not None:
        last_seen_dt = _parse_iso(loop.last_seen)
        if last_seen_dt is None:
            return False  # missing last_seen → fail-closed for this gate
        cutoff = now - timedelta(days=when.recent_days_max)
        if last_seen_dt < cutoff:
            return False
    return True


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
