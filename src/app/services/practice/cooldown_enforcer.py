"""Cooldown enforcer (spec §9.4).

Drop a candidate if the user has completed it within the last ``cooldown_hours``.
Fetches from ``practice_completion_repo.list_by_user_since`` with the cutoff.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from ...repositories.practice_completion_repo import PracticeCompletionRepo
from .catalog_loader import Practice


async def apply(
    candidates: Sequence[Practice],
    user_id: str,
    rule_cooldown_hours: int,
    *,
    completions_repo: PracticeCompletionRepo,
    now: Optional[datetime] = None,
) -> List[Practice]:
    if not candidates:
        return []
    n = now or datetime.now(timezone.utc)
    cutoff = n - timedelta(hours=rule_cooldown_hours)
    recent = await completions_repo.list_by_user_since(user_id, cutoff)
    recent_ids = {r.practice_id for r in recent}
    return [p for p in candidates if p.id not in recent_ids]
