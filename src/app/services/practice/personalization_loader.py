"""Loader for ``data/micro_practice/personalization.defaults.v1.json`` (spec §4.7).

Tunes the personalization scorer (helpfulness vote weight, time-of-day
match, recent-use penalty), the recency decay half-life, and the time-of-day
bucket boundaries used in personalization scoring (see spec §9.2).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from ..reflection import _config_io

_DEFAULT_RELATIVE_PATH = "src/app/data/micro_practice/personalization.defaults.v1.json"
_ENV_VAR = "REFLECTION_PERSONALIZATION_DEFAULTS_PATH"


class PersonalizationWeights(BaseModel):
    helpful_vote: float
    not_helpful_vote: float
    time_of_day_match: float
    recent_use_penalty: float


class PersonalizationDecay(BaseModel):
    recency_decay_half_life_days: float


class GlobalConfig(BaseModel):
    disallow_types: List[str] = Field(default_factory=list)


class UserFlagsDefault(BaseModel):
    no_breathwork: bool = False
    reduced_motion: bool = False
    private_mode: bool = False


class CooldownDefaults(BaseModel):
    default_hours: int = 12
    grief_hours: int = 24


class PersonalizationDefaults(BaseModel):
    """Top-level shape of personalization.defaults.v1.json.

    Note: ``time_of_day_buckets`` may include a leading ``_comment`` key in
    the JSON — those are stripped during validation. Each remaining bucket
    value must be a 2-element [start, end] list of hours in [0, 24).
    """

    version: int
    weights: PersonalizationWeights
    decay: PersonalizationDecay
    global_config: GlobalConfig = Field(alias="global")
    user_flags_default: UserFlagsDefault
    cooldowns: CooldownDefaults
    time_of_day_buckets: Dict[str, List[int]]

    model_config = {"populate_by_name": True}

    @field_validator("time_of_day_buckets", mode="before")
    @classmethod
    def _strip_comments(cls, v: Any, info: ValidationInfo) -> Any:
        """Remove keys starting with underscore (treated as comments)."""
        if not isinstance(v, dict):
            return v
        return {k: val for k, val in v.items() if not k.startswith("_")}

    @field_validator("time_of_day_buckets")
    @classmethod
    def _validate_bucket_ranges(cls, v: Dict[str, List[int]]) -> Dict[str, List[int]]:
        for name, hours in v.items():
            if not (isinstance(hours, list) and len(hours) == 2):
                raise ValueError(f"bucket '{name}' must be a [start, end] pair")
            start, end = hours
            for label, h in (("start", start), ("end", end)):
                if not isinstance(h, int) or not (0 <= h <= 24):
                    raise ValueError(
                        f"bucket '{name}' {label} must be int in [0, 24]; got {h!r}"
                    )
        return v


@lru_cache(maxsize=1)
def load_personalization_defaults() -> PersonalizationDefaults:
    """Parse + validate personalization.defaults.v1.json."""
    return _config_io.load_json_with_model(
        _ENV_VAR, _DEFAULT_RELATIVE_PATH, PersonalizationDefaults
    )


_config_io.register_clear(load_personalization_defaults.cache_clear)
