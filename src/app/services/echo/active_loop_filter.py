"""Active loop filter (spec §9.1).

A loop is considered "active" — and therefore eligible to drive practice
recommendations — if **any** of the following holds:

  1. Its intensity is at or above 0.60 AND its tone is rising or steady.
  2. It changed within the last 24h (``recently_changed=True``).
  3. Its tone is softening (always surfaced, regardless of intensity).

Pure function over the spec's ``LoopState`` shape — accepts duck-typed objects
that expose ``intensity_score``, ``tone_state``, and ``recently_changed``.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence

ACTIVE_INTENSITY_THRESHOLD = 0.60
RISING_AND_STEADY = frozenset({"rising", "steady"})


def filter_active(loops: Sequence) -> List:
    """Return the subset of ``loops`` that pass the active-loop rule."""
    out: List = []
    for loop in loops:
        if _is_active(loop):
            out.append(loop)
    return out


def _is_active(loop) -> bool:
    intensity = float(getattr(loop, "intensity_score", 0.0))
    tone = getattr(loop, "tone_state", "")
    recently_changed = bool(getattr(loop, "recently_changed", False))

    if tone == "softening":
        return True
    if recently_changed:
        return True
    return intensity >= ACTIVE_INTENSITY_THRESHOLD and tone in RISING_AND_STEADY
