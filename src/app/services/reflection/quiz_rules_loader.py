"""Loader for ``data/reflection/reflection_quiz_rules.v1.yaml`` (spec §4.1).

Public surface:
  * ``load_quiz_rules() -> QuizRules``  — cached parse + validation

The returned model is the source of truth for quiz weighting, allowed answers,
tie-break rules, and session timezone defaults.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, List

from pydantic import BaseModel, Field, field_validator

from . import _config_io

_DEFAULT_RELATIVE_PATH = "src/app/data/reflection/reflection_quiz_rules.v1.yaml"
_ENV_VAR = "REFLECTION_QUIZ_RULES_PATH"


class QuestionRules(BaseModel):
    """Per-question prompt + answer-to-tags map."""

    prompt: str
    answers: Dict[str, List[str]]


class TieBreakRules(BaseModel):
    use_q3: bool = True
    allow_user_override: bool = True


class SessionRules(BaseModel):
    default_tz: str = "America/New_York"


class QuizRules(BaseModel):
    """Top-level shape of reflection_quiz_rules.v1.yaml."""

    version: int
    weights: Dict[str, int]
    questions: Dict[str, QuestionRules]
    tie_break: TieBreakRules = Field(default_factory=TieBreakRules)
    session: SessionRules = Field(default_factory=SessionRules)

    @field_validator("weights")
    @classmethod
    def _weights_have_q1_q4(cls, v: Dict[str, int]) -> Dict[str, int]:
        required = {"q1", "q2", "q3", "q4"}
        missing = required - set(v.keys())
        if missing:
            raise ValueError(f"weights missing keys: {sorted(missing)}")
        return v

    @field_validator("questions")
    @classmethod
    def _questions_have_q1_q4(
        cls, v: Dict[str, QuestionRules]
    ) -> Dict[str, QuestionRules]:
        required = {"q1", "q2", "q3", "q4"}
        missing = required - set(v.keys())
        if missing:
            raise ValueError(f"questions missing keys: {sorted(missing)}")
        return v


@lru_cache(maxsize=1)
def load_quiz_rules() -> QuizRules:
    """Parse + validate the quiz rules YAML. Cached for the process lifetime."""
    return _config_io.load_yaml_with_model(_ENV_VAR, _DEFAULT_RELATIVE_PATH, QuizRules)


_config_io.register_clear(load_quiz_rules.cache_clear)
