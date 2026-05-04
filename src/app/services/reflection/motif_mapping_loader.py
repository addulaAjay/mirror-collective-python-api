"""Loader for ``data/reflection/motif_mapping.v1.json`` (spec §4.2).

Public surface:
  * ``load_motif_mapping() -> MotifMapping``
  * ``MotifMapping.lookup(tag) -> MotifEntry``  — single-tag lookup
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, Iterable, List

from pydantic import BaseModel

from ...core.exceptions import MotifNotFound
from . import _config_io

_DEFAULT_RELATIVE_PATH = "src/app/data/reflection/motif_mapping.v1.json"
_ENV_VAR = "REFLECTION_MOTIF_MAPPING_PATH"


class MotifEntry(BaseModel):
    """A single motif row keyed by tag in motif_mapping.v1.json."""

    motif_id: str
    motif_name: str
    icon: str
    element: str
    tone_tag: str
    why_text: str
    room_skin: str


class MotifMapping(BaseModel):
    """Top-level shape of motif_mapping.v1.json."""

    version: int
    motifs: Dict[str, MotifEntry]

    def lookup(self, tag: str) -> MotifEntry:
        """Return the motif row for ``tag`` or raise ``MotifNotFound``."""
        try:
            return self.motifs[tag]
        except KeyError as exc:
            raise MotifNotFound(f"Tag '{tag}' not found in motif mapping") from exc

    def lookup_by_motif_id(self, motif_id: str) -> MotifEntry:
        """Return the motif row whose ``motif_id`` matches.

        Used by ``PUT /me/reflection/room`` where the FE sends ``motif_id``
        (not the tag-key) to override the room skin.
        """
        for entry in self.motifs.values():
            if entry.motif_id == motif_id:
                return entry
        raise MotifNotFound(f"motif_id '{motif_id}' not found in motif mapping")

    def all_tags(self) -> List[str]:
        return list(self.motifs.keys())

    def all_entries(self) -> Iterable[MotifEntry]:
        return self.motifs.values()


@lru_cache(maxsize=1)
def load_motif_mapping() -> MotifMapping:
    """Parse + validate the motif mapping JSON. Cached for the process lifetime."""
    mapping = _config_io.load_json_with_model(
        _ENV_VAR, _DEFAULT_RELATIVE_PATH, MotifMapping
    )
    # Cross-check: motif_id should be unique across rows.
    seen: Dict[str, str] = {}
    for tag, entry in mapping.motifs.items():
        if entry.motif_id in seen:
            from ...core.exceptions import ConfigLoadError

            raise ConfigLoadError(
                f"Duplicate motif_id '{entry.motif_id}' on tags "
                f"'{seen[entry.motif_id]}' and '{tag}'"
            )
        seen[entry.motif_id] = tag
    return mapping


_config_io.register_clear(load_motif_mapping.cache_clear)
