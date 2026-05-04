"""Tag → MotifPayload mapping (spec §5.1, §6.1).

Wraps :func:`load_motif_mapping` and produces the API response payload that
``POST /reflection/quiz`` returns.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel

from ...core.exceptions import MotifNotFound
from .motif_mapping_loader import MotifEntry, load_motif_mapping


class MotifPayload(BaseModel):
    """Spec §5.1. Returned by ``POST /reflection/quiz``."""

    motif_id: str
    motif_name: str
    icon: str
    element: str
    tone_tag: str
    why_text: str
    room_skin: str
    scores: Dict[str, int]
    explanation: List[str]
    override_allowed: bool


def build_motif_payload(
    tag: str,
    scores: Dict[str, int],
    explanation: List[str],
    override_allowed: bool,
) -> MotifPayload:
    """Look up the motif row for ``tag`` and assemble a ``MotifPayload``."""
    entry: MotifEntry = load_motif_mapping().lookup(tag)
    return MotifPayload(
        motif_id=entry.motif_id,
        motif_name=entry.motif_name,
        icon=entry.icon,
        element=entry.element,
        tone_tag=entry.tone_tag,
        why_text=entry.why_text,
        room_skin=entry.room_skin,
        scores=scores,
        explanation=explanation,
        override_allowed=override_allowed,
    )


def build_payload_from_session(
    motif_id: str,
    scores: Dict[str, int],
    explanation: List[str],
    override_allowed: bool,
) -> MotifPayload:
    """Same as ``build_motif_payload`` but keyed by ``motif_id`` (used by the
    PUT /me/reflection/room override path)."""
    entry = load_motif_mapping().lookup_by_motif_id(motif_id)
    return MotifPayload(
        motif_id=entry.motif_id,
        motif_name=entry.motif_name,
        icon=entry.icon,
        element=entry.element,
        tone_tag=entry.tone_tag,
        why_text=entry.why_text,
        room_skin=entry.room_skin,
        scores=scores,
        explanation=explanation,
        override_allowed=override_allowed,
    )
