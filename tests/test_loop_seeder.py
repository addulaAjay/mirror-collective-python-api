"""Unit tests for loop_seeder (spec §B.2.2b, §4.8 + §8.3)."""

from __future__ import annotations

import pytest

from src.app.services.reflection.loop_seeder import LoopSeed, seed_loops_from_quiz
from src.app.services.reflection.quiz_to_loop_seeding_loader import (
    QuizToLoopSeeding,
    load_quiz_to_loop_seeding,
)


@pytest.fixture
def seeding() -> QuizToLoopSeeding:
    return load_quiz_to_loop_seeding()


# ============================================================
# Spec §B.2.2b table
# ============================================================


class TestSpiralCanonical:
    def test_seeds_two_loops(self, seeding):
        seeds = seed_loops_from_quiz(
            {"q1": "hopeful", "q2": "inspiration", "q3": "spiral", "q4": "insight"},
            seeding,
        )
        # Spec: agency rising and transition rising survive.
        loop_ids = sorted(s.loop_id for s in seeds)
        assert loop_ids == ["agency", "transition"]
        for s in seeds:
            assert s.tone_state == "rising"

    def test_intensity_scores_in_range(self, seeding):
        # Spec: both intensity_score between 0.65 and 0.85.
        seeds = seed_loops_from_quiz(
            {"q1": "hopeful", "q2": "inspiration", "q3": "spiral", "q4": "insight"},
            seeding,
        )
        for s in seeds:
            assert 0.65 <= s.intensity_score <= 0.85


class TestGroundedQ1ContributesNothing:
    def test_grounded_answer_has_empty_contribution_list(self, seeding):
        # Spec §4.8: "grounded" is settled-state, intentionally empty.
        assert seeding.contributions["q1"].answers["grounded"] == []

    def test_empty_answers_dict_returns_empty_seed_list(self, seeding):
        # If literally no answers are present, seeder returns no seeds.
        assert seed_loops_from_quiz({}, seeding) == []


class TestScatteredOverwhelmRising:
    def test_scattered_seeds_overwhelm_high_rising(self, seeding):
        # Spec §B.2.2b: q1=scattered, q2=peace, q3=waves, q4=presence.
        seeds = seed_loops_from_quiz(
            {"q1": "scattered", "q2": "peace", "q3": "waves", "q4": "presence"},
            seeding,
        )
        overwhelm_seeds = [s for s in seeds if s.loop_id == "overwhelm"]
        assert len(overwhelm_seeds) == 1
        # Q1 (rising 0.80) + Q2 (rising 0.50) outweigh Q3 (softening 0.60).
        assert overwhelm_seeds[0].tone_state == "rising"
        # "high intensity" → label High
        assert overwhelm_seeds[0].intensity_label == "High"


class TestToneTiebreakRisingPreferred:
    def test_synthetic_tone_tie_prefers_rising(self):
        # Construct a synthetic seeding config where (grief, rising) ==
        # (grief, steady) in raw_score.
        synthetic = QuizToLoopSeeding.model_validate(
            {
                "version": 1,
                "config": {
                    "top_n": 3,
                    "min_seed_score": 0.45,
                    "intensity_floor": 0.5,
                    "intensity_ceiling": 0.85,
                    "tone_tiebreak_priority": ["rising", "steady", "softening"],
                },
                "contributions": {
                    "q1": {
                        "weight": 1.0,
                        "answers": {
                            "x": [
                                {"loop": "grief", "tone": "rising", "score": 0.5},
                                {"loop": "grief", "tone": "steady", "score": 0.5},
                            ]
                        },
                    },
                    "q2": {"weight": 1.0, "answers": {"y": []}},
                    "q3": {"weight": 1.0, "answers": {"z": []}},
                    "q4": {"weight": 1.0, "answers": {"w": []}},
                },
            }
        )
        seeds = seed_loops_from_quiz(
            {"q1": "x", "q2": "y", "q3": "z", "q4": "w"}, synthetic
        )
        assert len(seeds) == 1
        assert seeds[0].loop_id == "grief"
        assert seeds[0].tone_state == "rising"


class TestMinSeedScoreFilter:
    def test_below_threshold_dropped(self):
        # Construct a contribution producing total = 0.40 — below min_seed_score=0.45.
        synthetic = QuizToLoopSeeding.model_validate(
            {
                "version": 1,
                "config": {
                    "top_n": 3,
                    "min_seed_score": 0.45,
                    "intensity_floor": 0.5,
                    "intensity_ceiling": 0.85,
                    "tone_tiebreak_priority": ["rising", "steady", "softening"],
                },
                "contributions": {
                    "q1": {
                        "weight": 1.0,
                        "answers": {
                            "x": [{"loop": "grief", "tone": "rising", "score": 0.40}]
                        },
                    },
                    "q2": {"weight": 1.0, "answers": {"y": []}},
                    "q3": {"weight": 1.0, "answers": {"z": []}},
                    "q4": {"weight": 1.0, "answers": {"w": []}},
                },
            }
        )
        seeds = seed_loops_from_quiz(
            {"q1": "x", "q2": "y", "q3": "z", "q4": "w"}, synthetic
        )
        assert seeds == []


class TestTopNLimit:
    def test_top_n_cuts_lowest_scoring_loops(self):
        # 5 loops above min_seed_score; top_n=3 keeps highest 3.
        synthetic = QuizToLoopSeeding.model_validate(
            {
                "version": 1,
                "config": {
                    "top_n": 3,
                    "min_seed_score": 0.45,
                    "intensity_floor": 0.5,
                    "intensity_ceiling": 0.85,
                    "tone_tiebreak_priority": ["rising", "steady", "softening"],
                },
                "contributions": {
                    "q1": {
                        "weight": 1.0,
                        "answers": {
                            "x": [
                                {"loop": "pressure", "tone": "rising", "score": 0.90},
                                {"loop": "overwhelm", "tone": "rising", "score": 0.80},
                                {"loop": "grief", "tone": "rising", "score": 0.70},
                                {
                                    "loop": "self_silencing",
                                    "tone": "rising",
                                    "score": 0.60,
                                },
                                {"loop": "agency", "tone": "rising", "score": 0.50},
                            ]
                        },
                    },
                    "q2": {"weight": 1.0, "answers": {"y": []}},
                    "q3": {"weight": 1.0, "answers": {"z": []}},
                    "q4": {"weight": 1.0, "answers": {"w": []}},
                },
            }
        )
        seeds = seed_loops_from_quiz(
            {"q1": "x", "q2": "y", "q3": "z", "q4": "w"}, synthetic
        )
        assert len(seeds) == 3
        loop_ids = [s.loop_id for s in seeds]
        # Top 3 by raw_score (desc)
        assert loop_ids == ["pressure", "overwhelm", "grief"]


class TestNormalizationRange:
    def test_intensity_within_floor_and_ceiling(self, seeding):
        seeds = seed_loops_from_quiz(
            {"q1": "scattered", "q2": "peace", "q3": "blocks", "q4": "direct"},
            seeding,
        )
        for s in seeds:
            assert 0.50 <= s.intensity_score <= 0.85

    def test_top_loop_lands_at_ceiling(self, seeding):
        # With max-relative normalization, the top scorer always equals ceiling.
        seeds = seed_loops_from_quiz(
            {"q1": "scattered", "q2": "peace", "q3": "blocks", "q4": "direct"},
            seeding,
        )
        if seeds:
            assert seeds[0].intensity_score == pytest.approx(0.85, abs=1e-3)


class TestLoopSeedToLoopState:
    def test_to_loop_state_assembles_correctly(self):
        seed = LoopSeed(
            loop_id="pressure",
            tone_state="rising",
            intensity_score=0.74,
            intensity_label="High",
            raw_score=1.20,
        )
        state = seed.to_loop_state(
            user_id="u1",
            last_seen_iso="2026-05-03T12:00:00Z",
            updated_at_iso="2026-05-03T12:00:00Z",
        )
        assert state.user_id == "u1"
        assert state.loop_id == "pressure"
        assert state.tone_state == "rising"
        assert state.intensity_score == 0.74
        assert state.intensity_label == "High"
        assert state.recently_changed is True
