"""Loader for ``data/reflection/quiz_to_loop_seeding.v1.yaml`` (spec §4.8).

This file is the producer of all loop state in V1 — the algorithm is fully
specified in spec §8.3 and uses these contributions plus a per-question
weight to seed the per-user (loop, tone, intensity) rows on every quiz
submission.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, List

from pydantic import BaseModel, Field, confloat

from . import _config_io

_DEFAULT_RELATIVE_PATH = "src/app/data/reflection/quiz_to_loop_seeding.v1.yaml"
_ENV_VAR = "REFLECTION_QUIZ_TO_LOOP_SEEDING_PATH"


SUPPORTED_LOOPS = frozenset(
    {"pressure", "overwhelm", "grief", "self_silencing", "agency", "transition"}
)
SUPPORTED_TONES = frozenset({"rising", "steady", "softening"})


class SeedingConfig(BaseModel):
    top_n: int = 3
    min_seed_score: confloat(ge=0.0, le=10.0) = 0.45
    intensity_floor: confloat(ge=0.0, le=1.0) = 0.50
    intensity_ceiling: confloat(ge=0.0, le=1.0) = 0.85
    tone_tiebreak_priority: List[str] = Field(
        default_factory=lambda: ["rising", "steady", "softening"]
    )


class Contribution(BaseModel):
    """One (loop, tone, score) row in an answer's contributions list."""

    loop: str
    tone: str
    score: confloat(ge=0.0, le=10.0)


class QuestionContributions(BaseModel):
    weight: confloat(ge=0.0, le=10.0)
    answers: Dict[str, List[Contribution]]


class QuizToLoopSeeding(BaseModel):
    version: int
    config: SeedingConfig
    contributions: Dict[str, QuestionContributions]


@lru_cache(maxsize=1)
def load_quiz_to_loop_seeding() -> QuizToLoopSeeding:
    """Parse + validate the seeding YAML. Cross-checks loop_ids and tone names."""
    parsed = _config_io.load_yaml_with_model(
        _ENV_VAR, _DEFAULT_RELATIVE_PATH, QuizToLoopSeeding
    )
    # Reject contributions referencing loops/tones outside the V1 set early.
    from ...core.exceptions import ConfigLoadError

    for q, qc in parsed.contributions.items():
        for ans, contribs in qc.answers.items():
            for c in contribs:
                if c.loop not in SUPPORTED_LOOPS:
                    raise ConfigLoadError(f"Unsupported loop '{c.loop}' in {q}/{ans}")
                if c.tone not in SUPPORTED_TONES:
                    raise ConfigLoadError(f"Unsupported tone '{c.tone}' in {q}/{ans}")
    return parsed


_config_io.register_clear(load_quiz_to_loop_seeding.cache_clear)
