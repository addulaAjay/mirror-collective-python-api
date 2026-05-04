"""Unit tests for motif_mapper (spec §B.2.2)."""

from __future__ import annotations

import pytest

from src.app.core.exceptions import MotifNotFound
from src.app.services.reflection.motif_mapper import (
    MotifPayload,
    build_motif_payload,
    build_payload_from_session,
)
from src.app.services.reflection.motif_mapping_loader import load_motif_mapping
from src.app.services.reflection.quiz_rules_loader import load_quiz_rules


def test_every_quiz_tag_has_motif_payload():
    """Every tag the quiz can produce must map to a complete MotifPayload."""
    rules = load_quiz_rules()
    all_tags = {
        tag
        for q in rules.questions.values()
        for tags in q.answers.values()
        for tag in tags
    }
    for tag in sorted(all_tags):
        payload = build_motif_payload(
            tag, scores={tag: 1}, explanation=[], override_allowed=False
        )
        assert isinstance(payload, MotifPayload)
        assert payload.motif_id, f"empty motif_id for tag {tag}"
        assert payload.motif_name, f"empty motif_name for tag {tag}"
        assert payload.room_skin, f"empty room_skin for tag {tag}"
        assert payload.scores == {tag: 1}


def test_evolution_tag_returns_spiral_payload():
    payload = build_motif_payload(
        "evolution",
        scores={"evolution": 5},
        explanation=["Q3=spiral (×2)"],
        override_allowed=False,
    )
    assert payload.motif_id == "spiral"
    assert payload.motif_name == "Spiral"
    assert payload.icon == "🌀"
    assert payload.element == "Fire"
    assert payload.tone_tag == "Evolution / Integration"
    assert payload.room_skin == "Spiral Room"


def test_unknown_tag_raises():
    with pytest.raises(MotifNotFound):
        build_motif_payload(
            "not_a_tag", scores={}, explanation=[], override_allowed=False
        )


def test_no_two_motifs_share_motif_id():
    mapping = load_motif_mapping()
    motif_ids = [e.motif_id for e in mapping.all_entries()]
    assert len(set(motif_ids)) == len(motif_ids)


def test_build_payload_from_session_via_motif_id():
    payload = build_payload_from_session(
        motif_id="mirror",
        scores={"reflection": 3},
        explanation=[],
        override_allowed=False,
    )
    assert payload.motif_id == "mirror"
    assert payload.motif_name == "Mirror"
    assert payload.room_skin == "Mirror Room"


def test_unknown_motif_id_raises():
    with pytest.raises(MotifNotFound):
        build_payload_from_session(
            motif_id="banana", scores={}, explanation=[], override_allowed=False
        )
