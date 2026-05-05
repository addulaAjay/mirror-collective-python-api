"""Apply state delta on practice completion (spec §8.3).

Behavior:
  * ``helpful=True``  → reduce target loop's ``intensity_score`` by 0.10
    (floor 0.0). If the cumulative drop within the last 24h is ≥ 0.05,
    set ``tone_state=softening`` and ``recently_changed=True``.
  * ``helpful=False`` → no intensity change, no tone change. The
    personalization scorer (Phase 5) will penalize this practice for
    future picks.
  * ``helpful=None``  → record completion only; no state change here.

Each successful helpful=True drop is 0.10, which always exceeds the 0.05
softening threshold — so a single helpful vote is enough to flip the tone to
softening (the celebration semantic the spec intends). The cumulative window
matters only at the floor, where the actual drop may be < 0.10 (e.g. dropping
from 0.03 → 0.0 is only a 0.03 delta; tone stays as-is).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from ...models.echo_loop_state import EchoLoopState
from ...repositories.echo_loop_state_repo import EchoLoopStateRepo
from ...repositories.practice_completion_repo import PracticeCompletionRepo
from .intensity_label_mapper import label_from_score

logger = logging.getLogger(__name__)

INTENSITY_DROP_PER_HELPFUL = 0.10
SOFTENING_THRESHOLD_24H = 0.05


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def apply_completion_delta(
    user_id: str,
    loop_id: str,
    helpful: Optional[bool],
    *,
    loop_state_repo: EchoLoopStateRepo,
    completions_repo: PracticeCompletionRepo,
    now: Optional[datetime] = None,
) -> Optional[EchoLoopState]:
    """Mutate the target loop's state per spec §8.3 and persist.

    Returns the updated state row (or ``None`` if no state exists / no mutation).
    """
    if helpful is not True:
        # helpful=False: spec says no state change — defer to personalizer.
        # helpful=None: no opinion yet — still no state change.
        return None

    state = await loop_state_repo.get(user_id, loop_id)
    if state is None:
        # No loop row to mutate — completion still recorded by the route handler.
        return None

    n = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    new_intensity = max(0.0, float(state.intensity_score) - INTENSITY_DROP_PER_HELPFUL)
    actual_drop = float(state.intensity_score) - new_intensity

    # Sum prior helpful drops within the last 24h for this loop.
    prior_drop = await _cumulative_helpful_drop_24h(
        user_id, loop_id, completions_repo, n
    )
    cumulative = prior_drop + actual_drop

    if cumulative >= SOFTENING_THRESHOLD_24H:
        state.tone_state = "softening"
        state.recently_changed = True

    state.intensity_score = round(new_intensity, 4)
    state.intensity_label = label_from_score(new_intensity)
    state.last_seen = _iso(n)
    state.updated_at = _iso(n)

    await loop_state_repo.upsert(state)
    return state


async def _cumulative_helpful_drop_24h(
    user_id: str,
    loop_id: str,
    completions_repo: PracticeCompletionRepo,
    now: datetime,
) -> float:
    """Sum of 0.10 for each prior ``helpful=True`` completion on this loop in
    the last 24h. (We use the constant rather than reading historical
    intensity values — this is consistent with the spec's per-completion drop
    of 0.10 even when the floor clamps it.)"""
    cutoff = now - timedelta(hours=24)
    completions = await completions_repo.list_by_user_since(user_id, cutoff)
    drops = sum(
        INTENSITY_DROP_PER_HELPFUL
        for c in completions
        if c.loop_id == loop_id and c.helpful is True
    )
    return float(drops)
