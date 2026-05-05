"""Unit tests for quiz_scorer (spec §7, §B.2.1)."""

from __future__ import annotations

import pytest

from src.app.core.exceptions import (
    ConfigLoadError,
    InvalidQuizAnswer,
    OverrideTagNotInTie,
)
from src.app.services.reflection.quiz_rules_loader import QuizRules, load_quiz_rules
from src.app.services.reflection.quiz_scorer import score_quiz


@pytest.fixture
def rules() -> QuizRules:
    return load_quiz_rules()


class TestCleanWinner:
    def test_canonical_spiral_quiz_wins_evolution(self, rules):
        # Spec §B.2.1 row 1: clean evolution-leaning answers.
        result = score_quiz(
            {"q1": "hopeful", "q2": "inspiration", "q3": "spiral", "q4": "insight"},
            rules,
        )
        assert result.winning_tag == "evolution"
        assert result.override_allowed is False
        assert result.tied_tags == []

    def test_weighting_q1_x1_q2_x2_q3_x2_q4_x1(self, rules):
        # Pure-evolution answers: q3=spiral (×2 → evolution) +
        # q2=inspiration (×2 → illumination, evolution) +
        # q4=insight (×1 → clarity, evolution).
        result = score_quiz(
            {"q1": "hopeful", "q2": "inspiration", "q3": "spiral", "q4": "insight"},
            rules,
        )
        # evolution = 2 (q2) + 2 (q3) + 1 (q4) = 5
        assert result.scores["evolution"] == 5
        # illumination = 1 (q1) + 2 (q2) = 3
        assert result.scores["illumination"] == 3
        # clarity = 1 (q4) = 1
        assert result.scores["clarity"] == 1
        # growth = 1 (q1) = 1 (no q2/q3/q4 contribution)
        assert result.scores["growth"] == 1


class TestQ3TieBreak:
    def test_q3_breaks_tie(self, rules):
        # Pick answers where two tags tie at top, but Q3's tag list contains
        # exactly one of them. We construct: structure vs growth tied at score 3.
        # q1=heavy → boundary, structure (each +1)
        # q2=clarity → clarity (+2)
        # q3=brick_stack → structure (+2)
        # q4=gentle → growth (+1)
        # Totals: structure=3, clarity=2, boundary=1, growth=1.
        # Wait — structure wins outright. Need a real tie.
        #
        # Try: q1=hopeful (growth+1, illumination+1)
        #       q2=inspiration (illumination+2, evolution+2)
        #       q3=feather (transition+2)
        #       q4=gentle (growth+1)
        # Totals: illumination=3, evolution=2, growth=2, transition=2.
        # illumination wins — no tie.
        #
        # Construct intentional tie via direct fixture:
        #   q1=hopeful, q2=inspiration, q3=radiant_burst, q4=gentle
        # Tags: q1: growth+1, illumination+1; q2: illumination+2, evolution+2;
        #       q3: illumination+2; q4: growth+1
        # Totals: illumination=5, evolution=2, growth=2.
        # illumination wins outright again. Score-only tie is hard with these
        # weights — leave the explicit Q3 tie-break test to the synthetic case
        # below.
        #
        # Instead use a synthetic rules object to force the tie.
        synthetic = QuizRules.model_validate(
            {
                "version": 1,
                "weights": {"q1": 1, "q2": 1, "q3": 1, "q4": 1},
                "questions": {
                    "q1": {"prompt": "x", "answers": {"a": ["alpha", "beta"]}},
                    "q2": {"prompt": "x", "answers": {"a": ["gamma"]}},
                    "q3": {"prompt": "x", "answers": {"a": ["beta"]}},
                    "q4": {"prompt": "x", "answers": {"a": ["alpha"]}},
                },
                "tie_break": {"use_q3": True, "allow_user_override": True},
                "session": {"default_tz": "America/New_York"},
            }
        )
        # Totals: alpha=2, beta=2, gamma=1. alpha vs beta tied at 2.
        # Q3 tag list = [beta] → beta wins.
        result = score_quiz({"q1": "a", "q2": "a", "q3": "a", "q4": "a"}, synthetic)
        assert result.winning_tag == "beta"
        assert result.override_allowed is False


class TestUnbreakableTie:
    def _synthetic_unbreakable_rules(self) -> QuizRules:
        # q3 contributes a different tag from the tied pair → Q3 doesn't break it.
        return QuizRules.model_validate(
            {
                "version": 1,
                "weights": {"q1": 1, "q2": 1, "q3": 1, "q4": 1},
                "questions": {
                    "q1": {"prompt": "x", "answers": {"a": ["alpha"]}},
                    "q2": {"prompt": "x", "answers": {"a": ["beta"]}},
                    "q3": {"prompt": "x", "answers": {"a": ["gamma"]}},
                    "q4": {"prompt": "x", "answers": {"a": ["alpha", "beta"]}},
                },
                "tie_break": {"use_q3": True, "allow_user_override": True},
                "session": {"default_tz": "America/New_York"},
            }
        )

    def test_unbreakable_tie_returns_override_allowed(self):
        rules = self._synthetic_unbreakable_rules()
        # Totals: alpha=2 (q1, q4), beta=2 (q2, q4), gamma=1 (q3).
        # Q3 tag list = [gamma] → can't disambiguate alpha vs beta.
        result = score_quiz({"q1": "a", "q2": "a", "q3": "a", "q4": "a"}, rules)
        assert result.override_allowed is True
        assert sorted(result.tied_tags) == ["alpha", "beta"]
        assert result.winning_tag == "alpha"  # deterministic alphabetical default

    def test_user_override_applied(self):
        rules = self._synthetic_unbreakable_rules()
        result = score_quiz(
            {"q1": "a", "q2": "a", "q3": "a", "q4": "a"},
            rules,
            user_override_tag="beta",
        )
        assert result.winning_tag == "beta"
        assert result.override_allowed is False

    def test_user_override_not_in_tied_set_raises(self):
        rules = self._synthetic_unbreakable_rules()
        with pytest.raises(OverrideTagNotInTie):
            score_quiz(
                {"q1": "a", "q2": "a", "q3": "a", "q4": "a"},
                rules,
                user_override_tag="gamma",
            )


class TestExplanation:
    def test_explanation_format(self, rules):
        result = score_quiz(
            {"q1": "hopeful", "q2": "inspiration", "q3": "spiral", "q4": "insight"},
            rules,
        )
        assert len(result.explanation) == 4
        assert result.explanation[0].startswith("Q1=hopeful")
        assert "×1" in result.explanation[0]
        assert result.explanation[2].startswith("Q3=spiral")
        assert "×2" in result.explanation[2]


class TestErrors:
    def test_invalid_answer_raises(self, rules):
        with pytest.raises(InvalidQuizAnswer):
            score_quiz(
                {"q1": "purple", "q2": "inspiration", "q3": "spiral", "q4": "insight"},
                rules,
            )

    def test_missing_answer_raises(self, rules):
        with pytest.raises(InvalidQuizAnswer):
            score_quiz(
                {"q1": "hopeful", "q2": "inspiration", "q3": "spiral"},
                rules,
            )
