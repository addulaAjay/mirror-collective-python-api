"""Safety filter (spec §9.3).

Drop candidate practices for which the user has opted out by type:

  * ``flags.no_breathwork=True`` removes any practice with ``type=breath``.
  * Each entry in ``user.disallow_types`` removes that type.
  * Each entry in ``personalization_defaults.global.disallow_types`` removes
    that type globally.

Pure function over the ``Practice`` catalog and the user's
``UserPersonalization`` row.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence

from ...models.user_personalization import UserPersonalization
from .catalog_loader import Practice


def apply(
    candidates: Sequence[Practice],
    prefs: UserPersonalization,
    *,
    global_disallow_types: Iterable[str] = (),
) -> List[Practice]:
    blocked = set()
    if prefs.flags.no_breathwork:
        blocked.add("breath")
    blocked.update(prefs.disallow_types or [])
    blocked.update(global_disallow_types or [])
    return [p for p in candidates if p.type not in blocked]
