"""Loader for ``data/micro_practice/micro_practices.v1.yaml`` (spec §4.5).

Public surface:
  * ``load_practice_catalog() -> PracticeCatalog``
  * ``PracticeCatalog.get(practice_id) -> Practice``

The catalog is the closed set of practices the recommender can return.
Each rule's ``candidates`` list must reference IDs from here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, List, Literal

from pydantic import BaseModel, conint

from ..reflection import _config_io

_DEFAULT_RELATIVE_PATH = "src/app/data/micro_practice/micro_practices.v1.yaml"
_ENV_VAR = "REFLECTION_PRACTICE_CATALOG_PATH"

PracticeType = Literal["breath", "somatic", "cognitive", "action", "reflection"]


class Practice(BaseModel):
    """One practice row from micro_practices.v1.yaml."""

    id: str
    title: str
    type: PracticeType
    duration_sec: conint(ge=0)
    steps: List[str]


class PracticeCatalog(BaseModel):
    version: int
    practices: List[Practice]

    def get(self, practice_id: str) -> Practice:
        """Lookup by practice ID. Raises KeyError on miss — callers should
        treat this as a config bug, not a request error."""
        for p in self.practices:
            if p.id == practice_id:
                return p
        raise KeyError(f"practice '{practice_id}' not found in catalog")

    def all_ids(self) -> List[str]:
        return [p.id for p in self.practices]


@lru_cache(maxsize=1)
def load_practice_catalog() -> PracticeCatalog:
    """Parse + validate micro_practices.v1.yaml. Enforces unique IDs."""
    parsed = _config_io.load_yaml_with_model(
        _ENV_VAR, _DEFAULT_RELATIVE_PATH, PracticeCatalog
    )
    seen: Dict[str, bool] = {}
    for p in parsed.practices:
        if p.id in seen:
            from ...core.exceptions import ConfigLoadError

            raise ConfigLoadError(f"Duplicate practice id '{p.id}' in catalog")
        seen[p.id] = True
    return parsed


_config_io.register_clear(load_practice_catalog.cache_clear)
