"""Unit tests for the heuristic Life Anchor detector (Phase 2B)."""

from __future__ import annotations

from src.app.services.life_anchor_detector import detect_life_anchor_candidate


def _result(mirror_moment: bool = False) -> dict:
    return {"change_detection": {"mirror_moment": mirror_moment}}


class TestKeywordMatches:
    def test_loss_is_sacred(self):
        out = detect_life_anchor_candidate(
            "My wife passed away three years ago.", _result()
        )
        assert out is not None
        assert out["anchor_type_guess"] == "loss"
        assert out["emotional_weight_guess"] == "sacred"
        assert "candidate_text" in out and out["candidate_text"]
        assert out["prompt"]

    def test_divorce_is_high(self):
        out = detect_life_anchor_candidate("We finalized the divorce.", _result())
        assert out is not None
        assert out["anchor_type_guess"] == "divorce"
        assert out["emotional_weight_guess"] == "high"

    def test_sobriety_is_high(self):
        out = detect_life_anchor_candidate("I've been sober for 90 days.", _result())
        assert out is not None
        assert out["anchor_type_guess"] == "sobriety"

    def test_transition_is_medium(self):
        out = detect_life_anchor_candidate("I just started a new job.", _result())
        assert out is not None
        assert out["anchor_type_guess"] == "transition"
        assert out["emotional_weight_guess"] == "medium"


class TestExplicitIntent:
    def test_remember_this_triggers(self):
        out = detect_life_anchor_candidate(
            "This matters. Please remember this.", _result()
        )
        assert out is not None
        assert out["anchor_type_guess"] == "custom"
        assert out["emotional_weight_guess"] == "high"


class TestMirrorMomentFallback:
    def test_mirror_moment_without_keyword_triggers(self):
        out = detect_life_anchor_candidate(
            "I think I finally understand myself.", _result(mirror_moment=True)
        )
        assert out is not None
        assert out["anchor_type_guess"] == "custom"
        assert out["emotional_weight_guess"] == "medium"


class TestNoMatch:
    def test_benign_message_no_moment_returns_none(self):
        assert (
            detect_life_anchor_candidate("What should I cook for dinner?", _result())
            is None
        )

    def test_empty_message_returns_none(self):
        assert detect_life_anchor_candidate("", _result(mirror_moment=True)) is None

    def test_missing_change_detection_is_safe(self):
        assert detect_life_anchor_candidate("just chatting", {}) is None
