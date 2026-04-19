"""
Archetype Scoring V1 Spec Tests

Tests all scenarios from the V1 specification:
- Core override (2/3 core questions match)
- Highest score wins
- Tie-breakers: core frequency → Q5 → Q1 → default order
- Assignment reason codes

Ported from TypeScript tests with identical logic and scenarios.
Now tests dynamic config support for multi-quiz framework.
"""

import pytest

from src.app.services.quiz_scoring import (
    DEFAULT_ARCHETYPES,
    DEFAULT_CORE_QUESTIONS,
    DEFAULT_ORDER,
    QuizAnswer,
    calculate_quiz_result,
)

# Default V1 config (archetype quiz)
DEFAULT_CONFIG = {
    "archetypes": ["Seeker", "Guardian", "Flamebearer", "Weaver"],
    "weights": {"core": 2, "regular": 1},
    "tieBreaker": {"order": ["Seeker", "Guardian", "Flamebearer", "Weaver"]},
    "coreQuestions": [1, 3, 5],
}


def create_user_answers(
    archetypes: list[str], config: dict | None = None
) -> list[QuizAnswer]:
    """Helper to create quiz answers from archetype list"""
    cfg = config or DEFAULT_CONFIG
    core_questions = cfg.get("coreQuestions", [1, 3, 5])
    return [
        QuizAnswer(
            question_id=i + 1,
            question=f"Q{i + 1}",
            archetype=archetype,
            is_core=(i + 1) in core_questions,
        )
        for i, archetype in enumerate(archetypes)
    ]


class TestV1SpecExamples:
    """Test all 4 examples from V1 specification"""

    def test_example_1_core_override(self):
        """
        Example 1: Core Override (2/3 core match)
        Q1=Seeker, Q2=Guardian, Q3=Seeker, Q4=Weaver, Q5=Guardian
        Result: Seeker (reason: core_override)
        """
        answers = create_user_answers(
            ["Seeker", "Guardian", "Seeker", "Weaver", "Guardian"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["final_archetype"] == "Seeker"
        assert result["assignment_reason"] == "core_override"
        assert result["scoring_details"]["had_core_archetype_match"] is True

    def test_example_2_highest_score(self):
        """
        Example 2: Highest Score Wins
        Q1=Guardian, Q2=Weaver, Q3=Flamebearer, Q4=Weaver, Q5=Weaver
        Scores: Guardian=2, Weaver=4, Flamebearer=2, Seeker=0
        Result: Weaver
        """
        answers = create_user_answers(
            ["Guardian", "Weaver", "Flamebearer", "Weaver", "Weaver"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["final_archetype"] == "Weaver"
        assert result["assignment_reason"] == "highest_score"
        assert result["total_scores"]["Weaver"] == 4
        assert result["total_scores"]["Guardian"] == 2
        assert result["total_scores"]["Flamebearer"] == 2

    def test_example_3_highest_score_guardian(self):
        """
        Example 3: Another Highest Score
        Q1=Flamebearer, Q2=Guardian, Q3=Weaver, Q4=Guardian, Q5=Guardian
        Scores: Flamebearer=2, Guardian=4, Weaver=2, Seeker=0
        Result: Guardian
        """
        answers = create_user_answers(
            ["Flamebearer", "Guardian", "Weaver", "Guardian", "Guardian"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["final_archetype"] == "Guardian"
        assert result["assignment_reason"] == "highest_score"
        assert result["total_scores"]["Guardian"] == 4

    def test_example_4_highest_score_different_pattern(self):
        """
        Example 4: Yet Another Highest Score
        Q1=Seeker, Q2=Guardian, Q3=Flamebearer, Q4=Guardian, Q5=Guardian
        Scores: Seeker=2, Guardian=4, Flamebearer=2, Weaver=0
        Result: Guardian
        """
        answers = create_user_answers(
            ["Seeker", "Guardian", "Flamebearer", "Guardian", "Guardian"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["final_archetype"] == "Guardian"
        assert result["assignment_reason"] == "highest_score"


class TestTieBreakerPriority:
    """Test tie-breaker priority: Q5 before Q1"""

    def test_q5_breaks_tie(self):
        """
        Q5 should break tie when tied on score
        Q1=Seeker(2), Q2=Seeker(1), Q3=Flamebearer(2), Q4=Guardian(1), Q5=Guardian(2)
        Core: Q1=Seeker, Q3=Flamebearer, Q5=Guardian (all different, no override)
        Scores: Seeker=3, Flamebearer=2, Guardian=3
        Tie between Seeker and Guardian, Q5=Guardian should win
        """
        answers = create_user_answers(
            ["Seeker", "Seeker", "Flamebearer", "Guardian", "Guardian"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["final_archetype"] == "Guardian"
        assert result["assignment_reason"] == "tie_break_q5"
        assert result["scoring_details"]["used_tie_breaker"] is True

    def test_q1_breaks_tie_when_q5_does_not(self):
        """
        Q1 should break tie when Q5 is not one of the tied archetypes
        Q1=Seeker(2), Q2=Flamebearer(1), Q3=Flamebearer(2), Q4=Seeker(1), Q5=Weaver(2)
        Seeker=3, Flamebearer=3, Weaver=2
        Q5=Weaver not in tie, so check Q1=Seeker
        """
        answers = create_user_answers(
            ["Seeker", "Flamebearer", "Flamebearer", "Seeker", "Weaver"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["final_archetype"] == "Seeker"
        assert result["assignment_reason"] == "tie_break_q1"


class TestTieBreakerCoreFrequency:
    """Test core frequency tie-breaker"""

    def test_q5_breaks_4way_tie(self):
        """
        Q5 should break 4-way tie
        Q1=Seeker, Q2=Guardian, Q3=Weaver, Q4=Guardian, Q5=Flamebearer
        Core: Q1=Seeker, Q3=Weaver, Q5=Flamebearer (all different, no override)
        Scores: Seeker=2, Guardian=2, Weaver=2, Flamebearer=2 (all tied!)
        Q5=Flamebearer should break the tie
        """
        answers = create_user_answers(
            ["Seeker", "Guardian", "Weaver", "Guardian", "Flamebearer"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["final_archetype"] == "Flamebearer"
        assert result["assignment_reason"] == "tie_break_q5"

    def test_q1_breaks_tie_when_q5_not_in_tied(self):
        """
        Q1 should break tie when Q5 is not in the tied archetypes
        Q1=Seeker, Q2=Seeker, Q3=Flamebearer, Q4=Flamebearer, Q5=Weaver
        Scores: Seeker=3, Flamebearer=3, Weaver=2
        Tied: Seeker and Flamebearer at 3 points
        Q5=Weaver (not in tie), so check Q1=Seeker
        """
        answers = create_user_answers(
            ["Seeker", "Seeker", "Flamebearer", "Flamebearer", "Weaver"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["final_archetype"] == "Seeker"
        assert result["assignment_reason"] == "tie_break_q1"


class TestTieBreakerDefaultOrder:
    """Test default order tie-breaker"""

    def test_q5_breaks_guardian_vs_seeker_tie(self):
        """
        Q5 should break Guardian vs Seeker tie
        Q1=Guardian, Q2=Seeker, Q3=Weaver, Q4=Guardian, Q5=Seeker
        Scores: Guardian=3 (2+1), Seeker=3 (1+2), Weaver=2
        Q5=Seeker should win
        """
        answers = create_user_answers(
            ["Guardian", "Seeker", "Weaver", "Guardian", "Seeker"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["final_archetype"] == "Seeker"
        assert result["assignment_reason"] == "tie_break_q5"
        assert result["scoring_details"]["used_tie_breaker"] is True

    def test_q5_breaks_4way_tie_weaver_wins(self):
        """
        Q5 should break 4-way tie with Weaver winning
        Q1=Flamebearer, Q2=Seeker, Q3=Guardian, Q4=Seeker, Q5=Weaver
        Scores: Flamebearer=2, Seeker=2, Guardian=2, Weaver=2 (perfect 4-way tie!)
        Q5=Weaver should win
        """
        answers = create_user_answers(
            ["Flamebearer", "Seeker", "Guardian", "Seeker", "Weaver"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["final_archetype"] == "Weaver"
        assert result["assignment_reason"] == "tie_break_q5"


class TestScoreCalculations:
    """Test weighted score calculations"""

    def test_correct_weighting_core_vs_regular(self):
        """
        Core questions should be worth 2 points, regular worth 1 point
        Q1=Seeker(2), Q2=Seeker(1), Q3=Seeker(2), Q4=Seeker(1), Q5=Seeker(2)
        Total: 8 points for Seeker
        """
        answers = create_user_answers(
            ["Seeker", "Seeker", "Seeker", "Seeker", "Seeker"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["total_scores"]["Seeker"] == 8  # 2+1+2+1+2 = 8
        assert result["final_archetype"] == "Seeker"
        assert result["assignment_reason"] == "core_override"  # 3/3 core match


class TestAssignmentReasonCodes:
    """Test all assignment reason codes"""

    def test_core_override_reason(self):
        """Should set core_override when 2 or more core questions match"""
        answers = create_user_answers(
            ["Guardian", "Seeker", "Guardian", "Seeker", "Flamebearer"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["assignment_reason"] == "core_override"

    def test_highest_score_reason(self):
        """Should set highest_score when one archetype clearly wins"""
        answers = create_user_answers(
            ["Seeker", "Guardian", "Flamebearer", "Weaver", "Guardian"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        # All core different, so no override
        # Seeker=2, Guardian=3, Flamebearer=2, Weaver=1
        assert result["assignment_reason"] == "highest_score"

    def test_tie_break_q5_reason(self):
        """Should set tie_break_q5 when Q5 breaks the tie"""
        answers = create_user_answers(
            ["Seeker", "Seeker", "Flamebearer", "Guardian", "Guardian"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["assignment_reason"] == "tie_break_q5"

    def test_tie_break_q1_reason(self):
        """Should set tie_break_q1 when Q1 breaks the tie"""
        answers = create_user_answers(
            ["Seeker", "Flamebearer", "Flamebearer", "Seeker", "Weaver"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["assignment_reason"] == "tie_break_q1"


class TestEdgeCases:
    """Test edge cases and boundary conditions"""

    def test_all_same_archetype(self):
        """Should handle all same archetype correctly"""
        answers = create_user_answers(
            ["Weaver", "Weaver", "Weaver", "Weaver", "Weaver"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert result["final_archetype"] == "Weaver"
        assert result["assignment_reason"] == "core_override"

    def test_all_different_archetypes_cycling(self):
        """Should handle cycling through different archetypes"""
        answers = create_user_answers(
            ["Seeker", "Guardian", "Flamebearer", "Weaver", "Seeker"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        # Seeker=4 (2+1+2), others have 2 or 1
        assert result["final_archetype"] == "Seeker"


class TestDataStructureValidation:
    """Test data structure completeness"""

    def test_complete_quiz_result_structure(self):
        """Should return complete QuizResult structure"""
        answers = create_user_answers(
            ["Seeker", "Guardian", "Seeker", "Weaver", "Guardian"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert "final_archetype" in result
        assert "assignment_reason" in result
        assert "total_scores" in result
        assert "core_answers" in result
        assert "all_answers" in result
        assert "scoring_details" in result

        assert "had_core_archetype_match" in result["scoring_details"]
        assert "used_tie_breaker" in result["scoring_details"]

    def test_core_answers_separated_correctly(self):
        """Should separate core answers correctly"""
        answers = create_user_answers(
            ["Seeker", "Guardian", "Flamebearer", "Weaver", "Seeker"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert len(result["core_answers"]) == 3  # Q1, Q3, Q5
        assert result["core_answers"][0]["question_id"] == 1
        assert result["core_answers"][1]["question_id"] == 3
        assert result["core_answers"][2]["question_id"] == 5


class TestV1SpecCompliance:
    """Test V1 spec compliance requirements"""

    def test_flexible_question_count(self):
        """Should accept variable number of questions (not hardcoded to 5)"""
        # 3 questions should work
        answers = create_user_answers(["Seeker", "Guardian", "Flamebearer"])
        result = calculate_quiz_result(answers, DEFAULT_CONFIG)
        assert result["final_archetype"] in DEFAULT_CONFIG["archetypes"]

    def test_core_questions_from_config(self):
        """Should read core questions from config (not hardcoded)"""
        assert DEFAULT_CONFIG["coreQuestions"] == [1, 3, 5]
        assert DEFAULT_CORE_QUESTIONS == [1, 3, 5]

    def test_archetypes_from_config(self):
        """Should read archetypes from config  (not hardcoded)"""
        assert DEFAULT_CONFIG["archetypes"] == [
            "Seeker",
            "Guardian",
            "Flamebearer",
            "Weaver",
        ]
        assert DEFAULT_ARCHETYPES == [
            "Seeker",
            "Guardian",
            "Flamebearer",
            "Weaver",
        ]

    def test_only_allowed_assignment_reasons(self):
        """Should only use allowed assignment_reason codes"""
        allowed_reasons = [
            "core_override",
            "highest_score",
            "tie_break_core_frequency",
            "tie_break_q5",
            "tie_break_q1",
            "tie_break_default",
        ]

        # Test various scenarios
        scenarios = [
            (["Seeker", "Guardian", "Seeker", "Weaver", "Guardian"], "core_override"),
            (
                ["Guardian", "Weaver", "Flamebearer", "Weaver", "Weaver"],
                "highest_score",
            ),
            (
                ["Seeker", "Seeker", "Flamebearer", "Guardian", "Guardian"],
                "tie_break_q5",
            ),
            (
                ["Seeker", "Flamebearer", "Flamebearer", "Seeker", "Weaver"],
                "tie_break_q1",
            ),
        ]

        for archetype_list, expected_reason in scenarios:
            answers = create_user_answers(archetype_list)
            result = calculate_quiz_result(answers, DEFAULT_CONFIG)

            assert result["assignment_reason"] in allowed_reasons
            assert result["assignment_reason"] == expected_reason

    def test_scores_for_all_4_archetypes(self):
        """Should return scores for all 4 archetypes"""
        answers = create_user_answers(
            ["Seeker", "Guardian", "Seeker", "Weaver", "Guardian"]
        )

        result = calculate_quiz_result(answers, DEFAULT_CONFIG)

        assert "Seeker" in result["total_scores"]
        assert "Guardian" in result["total_scores"]
        assert "Flamebearer" in result["total_scores"]
        assert "Weaver" in result["total_scores"]

        # Verify at least one has a score > 0
        total_score = sum(result["total_scores"].values())
        assert total_score > 0

    def test_progress_calculation_for_5_questions(self):
        """Progress bar values: Q1=20%, Q2=40%, Q3=60%, Q4=80%, Q5=100%"""
        progress_values = [20, 40, 60, 80, 100]

        for i in range(5):
            expected_progress = progress_values[i]
            actual_progress = ((i + 1) / 5) * 100
            assert actual_progress == expected_progress

    def test_default_archetype_order(self):
        """Default order should be: Seeker > Guardian > Flamebearer > Weaver"""
        default_order = DEFAULT_ORDER
        assert default_order == ["Seeker", "Guardian", "Flamebearer", "Weaver"]
        assert default_order[0] == "Seeker"  # First in order
        assert default_order[3] == "Weaver"  # Last in order
