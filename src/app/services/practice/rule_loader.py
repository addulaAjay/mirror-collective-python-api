"""Loader for ``data/micro_practice/echo_practice_rules.v1.yaml`` (spec §4.4).

Public surface:
  * ``load_practice_rules() -> PracticeRulesDoc``

V1 contains exactly 6 rules — all gating on ``loop_id`` only. ``motif_any``
and ``narrative_stage_in`` are reserved for V2 and ignored here (per spec
§8.4 forward compatibility note).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, confloat, conint

from ..reflection import _config_io

_DEFAULT_RELATIVE_PATH = "src/app/data/micro_practice/echo_practice_rules.v1.yaml"
_ENV_VAR = "REFLECTION_PRACTICE_RULES_PATH"


class RuleWhen(BaseModel):
    """The conditions a single rule matches against a target LoopState."""

    loop_id: str
    min_strength: Optional[confloat(ge=0.0, le=1.0)] = None
    trend_in: List[str] = Field(default_factory=list)
    recent_days_max: Optional[conint(ge=0)] = None
    # V2-reserved fields (see spec §8.4 forward compatibility):
    motif_any: Optional[List[str]] = None
    narrative_stage_in: Optional[List[str]] = None


class PracticeRule(BaseModel):
    id: str
    when: RuleWhen
    candidates: List[str]
    cooldown_hours: conint(ge=0)
    priority: int


class FallbackConfig(BaseModel):
    enabled: bool = True
    default_practice_id: str
    alternate_for_no_breathwork_id: str
    rule_id: str = "fallback"


class FamilyFallback(BaseModel):
    """Per-family (spec §6) fallback pool for one loop family.

    ``candidates`` is the pool to draw from when no primary rule matches (or
    every matched rule's candidates were filtered). ``cooldown_hours`` applies
    the family cooldown (12h default; grief 24h; transition 18h)."""

    candidates: List[str]
    cooldown_hours: conint(ge=0)


class PerFamilyFallbackConfig(BaseModel):
    """Spec §6 starred per-family fallbacks (reversible via ``enabled``).

    When ``enabled`` is False the recommender keeps the legacy GLOBAL
    ``fallback`` block. When True, a family with no matching primary rule draws
    from its own ``families`` pool; a family absent from ``families`` yields a
    typed ``no_eligible_practice`` reason instead of substituting a different
    family."""

    enabled: bool = False
    rule_id: str = "family_fallback"
    families: Dict[str, FamilyFallback] = Field(default_factory=dict)


class PracticeRulesDoc(BaseModel):
    version: int
    rules: List[PracticeRule]
    fallback: FallbackConfig
    per_family_fallback: PerFamilyFallbackConfig = Field(
        default_factory=PerFamilyFallbackConfig
    )

    def rule_by_id(self, rule_id: str) -> Optional[PracticeRule]:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None


@lru_cache(maxsize=1)
def load_practice_rules() -> PracticeRulesDoc:
    """Parse + validate echo_practice_rules.v1.yaml."""
    return _config_io.load_yaml_with_model(
        _ENV_VAR, _DEFAULT_RELATIVE_PATH, PracticeRulesDoc
    )


_config_io.register_clear(load_practice_rules.cache_clear)
