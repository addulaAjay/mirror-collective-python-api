"""Unit tests for room_skin_resolver (spec §6.5)."""

from __future__ import annotations

import pytest

from src.app.core.exceptions import MotifNotFound, OverrideNotAllowed
from src.app.models.reflection_session import ReflectionSession
from src.app.services.reflection.room_skin_resolver import resolve_override


def _session(override_allowed: bool) -> ReflectionSession:
    return ReflectionSession(
        user_id="u1",
        motif_id="spiral",
        motif_name="Spiral",
        room_skin="Spiral Room",
        motif_payload={
            "motif_id": "spiral",
            "motif_name": "Spiral",
            "icon": "🌀",
            "element": "Fire",
            "tone_tag": "Evolution / Integration",
            "why_text": "...",
            "room_skin": "Spiral Room",
            "scores": {"evolution": 5},
            "explanation": [],
            "override_allowed": override_allowed,
        },
    )


def test_override_resolves_to_target_motif_entry():
    session = _session(override_allowed=True)
    entry = resolve_override(session, "mirror")
    assert entry.motif_id == "mirror"
    assert entry.motif_name == "Mirror"
    assert entry.room_skin == "Mirror Room"


def test_override_blocked_when_no_tie_in_session():
    session = _session(override_allowed=False)
    with pytest.raises(OverrideNotAllowed):
        resolve_override(session, "mirror")


def test_unknown_motif_id_raises():
    session = _session(override_allowed=True)
    with pytest.raises(MotifNotFound):
        resolve_override(session, "banana")


def test_session_without_motif_payload_blocks_override():
    session = ReflectionSession(motif_id="spiral")
    # motif_payload is empty dict by default → override_allowed treated as False.
    with pytest.raises(OverrideNotAllowed):
        resolve_override(session, "mirror")
