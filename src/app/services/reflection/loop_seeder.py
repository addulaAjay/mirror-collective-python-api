"""Quiz-driven loop-state seeder (spec §4.8 + §8.3).

Producer of all loop state in V1. Pure function — returns the seed list; the
route handler is responsible for upserting via ``EchoLoopStateRepo``.

Algorithm summary:
    1. Accumulate (loop, tone) buckets weighted by ``question_weight × score``
    2. Per loop, collapse to its highest-scoring tone (tie-break order: rising,
       steady, softening per ``config.tone_tiebreak_priority``)
    3. Drop loops below ``min_seed_score``
    4. Take top ``top_n`` by raw score
    5. Normalize raw score to ``[intensity_floor, intensity_ceiling]``

Normalization detail (spec is not prescriptive):
    Scaled relative to the highest surviving raw score:
        intensity_score = floor + (ceiling - floor) * (raw / max_raw)
    Clamped to [floor, ceiling]. The top-scoring loop always lands at
    ``ceiling`` (0.85). Lower loops scale down proportionally. This satisfies
    spec test fixtures B.2.2b (canonical Spiral both in [0.65, 0.85];
    scattered overwhelm at "High" label). V2 may revisit with absolute scaling.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ...models.echo_loop_state import EchoLoopState
from ..echo.intensity_label_mapper import label_from_score
from .quiz_to_loop_seeding_loader import QuizToLoopSeeding


@dataclass(frozen=True)
class LoopSeed:
    """Output row from the seeder. Convertible to ``EchoLoopState``."""

    loop_id: str
    tone_state: str
    intensity_score: float
    intensity_label: str
    raw_score: float

    def to_loop_state(
        self, user_id: str, last_seen_iso: str, updated_at_iso: str
    ) -> EchoLoopState:
        return EchoLoopState(
            user_id=user_id,
            loop_id=self.loop_id,
            tone_state=self.tone_state,
            intensity_score=self.intensity_score,
            intensity_label=self.intensity_label,
            last_seen=last_seen_iso,
            recently_changed=True,
            narrative_stage=None,
            updated_at=updated_at_iso,
        )


def seed_loops_from_quiz(
    answers: Dict[str, str],
    seeding: QuizToLoopSeeding,
) -> List[LoopSeed]:
    """Run the §8.3 algorithm. Returns 0..top_n seeds, sorted desc by intensity."""
    # 1. Accumulate (loop, tone) buckets.
    buckets: Dict[Tuple[str, str], float] = defaultdict(float)
    for q in ("q1", "q2", "q3", "q4"):
        ans = answers.get(q)
        if ans is None:
            continue
        contributions = seeding.contributions.get(q)
        if contributions is None:
            continue
        weight = float(contributions.weight)
        per_answer = contributions.answers.get(ans, [])
        for c in per_answer:
            key = (c.loop, c.tone)
            buckets[key] += float(c.score) * weight

    if not buckets:
        return []

    # 2. Collapse tone-state collisions per loop (spec §8.3 step 3).
    tone_priority = list(seeding.config.tone_tiebreak_priority)
    by_loop: Dict[str, Tuple[str, float]] = {}  # loop_id -> (tone, score)
    for (loop_id, tone), score in buckets.items():
        existing = by_loop.get(loop_id)
        if (
            existing is None
            or score > existing[1]
            or (
                score == existing[1]
                and tone_priority.index(tone) < tone_priority.index(existing[0])
            )
        ):
            by_loop[loop_id] = (tone, score)

    # 3. Drop below min_seed_score; sort desc; take top_n.
    survivors = [
        (loop_id, tone, score)
        for loop_id, (tone, score) in by_loop.items()
        if score >= seeding.config.min_seed_score
    ]
    survivors.sort(key=lambda t: t[2], reverse=True)
    survivors = survivors[: seeding.config.top_n]

    # 4. Normalize relative to the top-scoring survivor.
    floor = float(seeding.config.intensity_floor)
    ceiling = float(seeding.config.intensity_ceiling)
    span = ceiling - floor
    max_raw = max((s for _, _, s in survivors), default=0.0)

    seeds: List[LoopSeed] = []
    for loop_id, tone, raw in survivors:
        normalized = (raw / max_raw) if max_raw > 0 else 1.0
        intensity = floor + span * normalized
        intensity = max(floor, min(ceiling, intensity))
        seeds.append(
            LoopSeed(
                loop_id=loop_id,
                tone_state=tone,
                intensity_score=round(intensity, 4),
                intensity_label=label_from_score(intensity),
                raw_score=round(raw, 4),
            )
        )
    return seeds
