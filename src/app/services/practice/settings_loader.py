"""Loader for ``data/micro_practice/micro_practice.settings.v1.yaml`` (spec §4.6).

Tiny config — just operator-level toggles for the recommender.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, Field, conint

from ..reflection import _config_io

_DEFAULT_RELATIVE_PATH = "src/app/data/micro_practice/micro_practice.settings.v1.yaml"
_ENV_VAR = "REFLECTION_MICRO_PRACTICE_SETTINGS_PATH"


class MicroPracticeDefaults(BaseModel):
    cooldown_hours_default: conint(ge=0) = 12
    cooldown_hours_grief: conint(ge=0) = 24
    fallback_enabled: bool = True
    max_practices_per_session: conint(ge=0) = 3
    snapshot_refresh_after_completion: bool = True


class MicroPracticeSettings(BaseModel):
    version: int
    defaults: MicroPracticeDefaults = Field(default_factory=MicroPracticeDefaults)


@lru_cache(maxsize=1)
def load_micro_practice_settings() -> MicroPracticeSettings:
    """Parse + validate the settings YAML."""
    return _config_io.load_yaml_with_model(
        _ENV_VAR, _DEFAULT_RELATIVE_PATH, MicroPracticeSettings
    )


_config_io.register_clear(load_micro_practice_settings.cache_clear)
