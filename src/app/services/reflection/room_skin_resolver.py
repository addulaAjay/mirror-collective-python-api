"""Room-skin override validation (spec §6.5).

Given an active session and a target ``motif_id``, validate that the override
is permitted (the session's quiz must have produced a tie, recorded as
``override_allowed=True`` in the persisted ``motif_payload``) and the
``motif_id`` exists in motif_mapping.v1.json. Returns the resolved
``MotifEntry`` for the new skin.
"""

from __future__ import annotations

from typing import Tuple

from ...core.exceptions import OverrideNotAllowed
from ...models.reflection_session import ReflectionSession
from .motif_mapping_loader import MotifEntry, load_motif_mapping


def resolve_override(session: ReflectionSession, target_motif_id: str) -> MotifEntry:
    """Validate + resolve a room-skin override.

    Raises:
        OverrideNotAllowed: session's quiz produced a unique winner.
        MotifNotFound: target motif_id is not in the mapping.
    """
    if not _override_was_offered(session):
        raise OverrideNotAllowed(
            "this session's quiz did not produce a tie; override not allowed"
        )
    return load_motif_mapping().lookup_by_motif_id(target_motif_id)


def _override_was_offered(session: ReflectionSession) -> bool:
    """True iff the session's stored motif_payload says ``override_allowed=True``."""
    if not session.motif_payload:
        return False
    return bool(session.motif_payload.get("override_allowed", False))
