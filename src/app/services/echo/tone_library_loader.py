"""Loader for ``data/micro_practice/echo_signature_tone_library.v1.yaml`` (spec §4.3).

Public surface:
  * ``load_tone_library() -> ToneLibrary``
  * ``ToneLibrary.lookup(loop_id, tone_state) -> ToneEntry``

The Echo Signature card front (and the snapshot endpoint's per-loop
``icon`` / ``reflection_line`` fields) read from here. 18 entries:
6 loops × 3 tone states.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Dict, List

from pydantic import BaseModel

from ..reflection import _config_io

_DEFAULT_RELATIVE_PATH = (
    "src/app/data/micro_practice/echo_signature_tone_library.v1.yaml"
)
_ENV_VAR = "REFLECTION_TONE_LIBRARY_PATH"

_REQUIRED_LOOPS = [
    "pressure",
    "overwhelm",
    "grief",
    "self_silencing",
    "agency",
    "transition",
]
_REQUIRED_TONES = ["rising", "steady", "softening"]


class ToneEntry(BaseModel):
    """The (icon, reflection_line) pair for a single (loop, tone)."""

    icon: str
    reflection_line: str


class _ToneCell(BaseModel):
    """Per-tone-state cell inside a loop block."""

    reflection_line: str


class _LoopBlock(BaseModel):
    """A loop's icon, label, and 3 tone cells."""

    icon: str
    label: str
    tones: Dict[str, _ToneCell]


class ToneLibrary(BaseModel):
    """Top-level shape of echo_signature_tone_library.v1.yaml."""

    version: int
    loops: Dict[str, _LoopBlock]

    def lookup(self, loop_id: str, tone_state: str) -> ToneEntry:
        """Return ``(icon, reflection_line)`` for a given (loop, tone)."""
        block = self.loops.get(loop_id)
        if block is None:
            raise KeyError(f"tone library has no loop '{loop_id}'")
        cell = block.tones.get(tone_state)
        if cell is None:
            raise KeyError(
                f"tone library has no tone '{tone_state}' for loop '{loop_id}'"
            )
        return ToneEntry(icon=block.icon, reflection_line=cell.reflection_line)

    def label_for(self, loop_id: str) -> str:
        block = self.loops.get(loop_id)
        if block is None:
            raise KeyError(f"tone library has no loop '{loop_id}'")
        return block.label

    def all_loops(self) -> List[str]:
        return list(self.loops.keys())


@lru_cache(maxsize=1)
def load_tone_library() -> ToneLibrary:
    """Parse + validate the tone library YAML.

    Enforces presence of all 6 loops × 3 tones — partial libraries fail at
    boot, not at request time.
    """
    parsed = _config_io.load_yaml_with_model(
        _ENV_VAR, _DEFAULT_RELATIVE_PATH, ToneLibrary
    )
    from ...core.exceptions import ConfigLoadError

    missing_loops = [l for l in _REQUIRED_LOOPS if l not in parsed.loops]
    if missing_loops:
        raise ConfigLoadError(f"tone library missing loops: {missing_loops}")
    for loop_id, block in parsed.loops.items():
        missing_tones = [t for t in _REQUIRED_TONES if t not in block.tones]
        if missing_tones:
            raise ConfigLoadError(
                f"tone library loop '{loop_id}' missing tones: {missing_tones}"
            )
    return parsed


_config_io.register_clear(load_tone_library.cache_clear)
