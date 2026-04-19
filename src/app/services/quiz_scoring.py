"""
Dynamic Quiz Scoring Engine

Implements weighted scoring with core question priority and tie-breaking logic
for any quiz type (archetypes, learning styles, personality types, etc.).

Scoring Rules (configurable per quiz):
- Core questions: Weighted higher (default: 2 points each)
- Regular questions: Standard weight (default: 1 point each)
- All weights, categories, and questions configurable via quiz config

Assignment Logic:
1. Core Override: If same category appears in 2+ core questions → assign immediately
2. Highest Score: Category with most points wins
3. Tie-Breakers (in order):
   a. Core frequency: Which tied category appears more in core questions
   b. Last core question: If that category is in tie, it wins
   c. First core question: If that category is in tie, it wins
   d. Default order: From quiz config tie-breaker rules

Assignment Reasons:
- core_override: Multiple core questions matched same category
- highest_score: One category clearly won by points
- tie_break_core_frequency: Tie broken by core question frequency
- tie_break_q5: Tie broken by last core question answer
- tie_break_q1: Tie broken by first core question answer
- tie_break_default: Tie broken by configured priority order
"""

from typing import Any, Dict, List, Literal, Optional, TypedDict

# Type definitions
AssignmentReason = Literal[
    "core_override",
    "highest_score",
    "tie_break_core_frequency",
    "tie_break_q5",
    "tie_break_q1",
    "tie_break_default",
]


class QuizAnswer(TypedDict):
    """Single quiz answer"""

    question_id: int
    question: str
    archetype: (
        str  # Result category (archetype, learning style, personality type, etc.)
    )
    is_core: bool


class QuizResult(TypedDict):
    """Complete quiz result with scoring details"""

    final_archetype: str  # Winning category (dynamic based on quiz config)
    assignment_reason: AssignmentReason
    total_scores: Dict[str, int]  # Scores for all categories
    core_answers: List[QuizAnswer]
    all_answers: List[QuizAnswer]
    scoring_details: Dict[str, bool]


# Default configuration (fallback if config not provided)
DEFAULT_ARCHETYPES: List[str] = ["Seeker", "Guardian", "Flamebearer", "Weaver"]
DEFAULT_ORDER: List[str] = ["Seeker", "Guardian", "Flamebearer", "Weaver"]
DEFAULT_CORE_QUESTIONS: List[int] = [1, 3, 5]
DEFAULT_CORE_WEIGHT: int = 2
DEFAULT_REGULAR_WEIGHT: int = 1


def _check_core_override(
    core_answers: List[QuizAnswer], archetypes: List[str]
) -> str | None:
    """
    Check if 2 or more core questions have the same category

    Args:
        core_answers: Answers to core questions
        archetypes: List of valid result categories from config

    Returns:
        Category if override applies, None otherwise
    """
    archetype_counts: Dict[str, int] = {arch: 0 for arch in archetypes}

    for answer in core_answers:
        if answer["archetype"] in archetype_counts:
            archetype_counts[answer["archetype"]] += 1

    # Check if any archetype appears 2 or more times
    for archetype, count in archetype_counts.items():
        if count >= 2:
            return archetype

    return None


def _calculate_scores(
    answers: List[QuizAnswer],
    core_questions: List[int],
    core_weight: int,
    regular_weight: int,
    archetypes: List[str],
) -> Dict[str, int]:
    """
    Calculate weighted scores for all categories using config weights

    Args:
        answers: All quiz answers
        core_questions: List of core question IDs from config
        core_weight: Weight for core questions from config
        regular_weight: Weight for regular questions from config
        archetypes: List of valid result categories from config

    Returns:
        Dict mapping each category to its total score
    """
    scores: Dict[str, int] = {arch: 0 for arch in archetypes}

    for answer in answers:
        archetype = answer["archetype"]
        if archetype in scores:
            weight = (
                core_weight
                if answer["question_id"] in core_questions
                else regular_weight
            )
            scores[archetype] += weight

    return scores


def _break_tie(
    tied_archetypes: List[str],
    core_answers: List[QuizAnswer],
    all_answers: List[QuizAnswer],
    core_questions: List[int],
    tie_breaker_order: List[str],
) -> tuple[str, AssignmentReason]:
    """
    Break tie using config-defined priority order:
    1. Core frequency (which appears more in core questions)
    2. Last core question answer (if in tie)
    3. First core question answer (if in tie)
    4. Default order from config

    Args:
        tied_archetypes: Archetypes with tied scores
        core_answers: Answers to core questions
        all_answers: All quiz answers
        core_questions: Core question IDs from config (sorted)
        tie_breaker_order: Tie-breaker order from config

    Returns:
        (winning_archetype, assignment_reason)
    """
    # Tie-breaker 1: Core frequency
    core_counts: Dict[str, int] = {arch: 0 for arch in tied_archetypes}
    for answer in core_answers:
        if answer["archetype"] in tied_archetypes:
            core_counts[answer["archetype"]] += 1

    max_core_count = max(core_counts.values())
    archetypes_with_max_core = [
        arch for arch, count in core_counts.items() if count == max_core_count
    ]

    if len(archetypes_with_max_core) == 1:
        return archetypes_with_max_core[0], "tie_break_core_frequency"

    # Tie-breaker 2: Last core question answer (dynamic based on config)
    sorted_core_questions = sorted(core_questions, reverse=True)
    last_core_q = sorted_core_questions[0] if sorted_core_questions else None
    if last_core_q:
        last_core_answer = next(
            (a for a in all_answers if a["question_id"] == last_core_q), None
        )
        if last_core_answer and last_core_answer["archetype"] in tied_archetypes:
            return last_core_answer["archetype"], "tie_break_q5"

    # Tie-breaker 3: First core question answer (dynamic based on config)
    sorted_core_questions_asc = sorted(core_questions)
    first_core_q = sorted_core_questions_asc[0] if sorted_core_questions_asc else None
    if first_core_q:
        first_core_answer = next(
            (a for a in all_answers if a["question_id"] == first_core_q), None
        )
        if first_core_answer and first_core_answer["archetype"] in tied_archetypes:
            return first_core_answer["archetype"], "tie_break_q1"

    # Tie-breaker 4: Default order from config
    for archetype in tie_breaker_order:
        if archetype in tied_archetypes:
            return archetype, "tie_break_default"

    # Fallback (should never reach here)
    return tied_archetypes[0], "tie_break_default"


def calculate_quiz_result(
    answers: List[QuizAnswer], quiz_config: Optional[Dict[str, Any]] = None
) -> QuizResult:
    """
    Calculate quiz result from user answers using dynamic config

    Args:
        answers: List of quiz answers with category mappings
        quiz_config: Quiz configuration from questions.json with:
            - archetypes: List of result category names
            - weights: {core: int, regular: int}
            - tieBreaker: {order: List[category names]}
            - coreQuestions: List[question IDs] (optional, defaults to [1,3,5])

    Returns:
        QuizResult with final category and scoring details
    """
    # Use config or defaults
    if quiz_config:
        archetypes = quiz_config.get("archetypes", DEFAULT_ARCHETYPES)
        core_weight = quiz_config.get("weights", {}).get("core", DEFAULT_CORE_WEIGHT)
        regular_weight = quiz_config.get("weights", {}).get(
            "regular", DEFAULT_REGULAR_WEIGHT
        )
        tie_breaker_order = quiz_config.get("tieBreaker", {}).get(
            "order", DEFAULT_ORDER
        )
        core_questions = quiz_config.get("coreQuestions", DEFAULT_CORE_QUESTIONS)
    else:
        archetypes = DEFAULT_ARCHETYPES
        core_weight = DEFAULT_CORE_WEIGHT
        regular_weight = DEFAULT_REGULAR_WEIGHT
        tie_breaker_order = DEFAULT_ORDER
        core_questions = DEFAULT_CORE_QUESTIONS

    if len(answers) < 1:
        raise ValueError(f"Expected at least 1 answer, got {len(answers)}")

    # Separate core and regular answers based on config
    core_answers = [a for a in answers if a["question_id"] in core_questions]

    if not core_answers:
        raise ValueError(
            f"Expected at least 1 core question from {core_questions}, got {len(core_answers)}"
        )

    # Step 1: Check for core override (2+ core questions match)
    core_override_result = _check_core_override(core_answers, archetypes)
    if core_override_result:
        scores = _calculate_scores(
            answers, core_questions, core_weight, regular_weight, archetypes
        )
        return QuizResult(
            final_archetype=core_override_result,
            assignment_reason="core_override",
            total_scores=scores,
            core_answers=core_answers,
            all_answers=answers,
            scoring_details={
                "had_core_archetype_match": True,
                "used_tie_breaker": False,
            },
        )

    # Step 2: Calculate total scores using config weights
    scores = _calculate_scores(
        answers, core_questions, core_weight, regular_weight, archetypes
    )

    # Step 3: Find highest score
    max_score = max(scores.values())
    tied_archetypes = [
        archetype for archetype, score in scores.items() if score == max_score
    ]

    # If single winner, return it
    if len(tied_archetypes) == 1:
        return QuizResult(
            final_archetype=tied_archetypes[0],
            assignment_reason="highest_score",
            total_scores=scores,
            core_answers=core_answers,
            all_answers=answers,
            scoring_details={
                "had_core_archetype_match": False,
                "used_tie_breaker": False,
            },
        )

    # Step 4: Break tie using config-defined rules
    final_archetype, reason = _break_tie(
        tied_archetypes, core_answers, answers, core_questions, tie_breaker_order
    )

    return QuizResult(
        final_archetype=final_archetype,
        assignment_reason=reason,
        total_scores=scores,
        core_answers=core_answers,
        all_answers=answers,
        scoring_details={
            "had_core_archetype_match": False,
            "used_tie_breaker": True,
        },
    )
