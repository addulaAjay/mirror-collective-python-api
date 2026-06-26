"""Map and validate submitted quiz answers before scoring.

Pulled out of the route handler so the mapping/validation logic is pure and
unit-testable. Responsibilities:

- Robustly extract the selected option text from text, ImageAnswer, or dict
  answers (an image answer is a pydantic model, not a dict).
- De-duplicate answers by question id, keeping the user's last choice, so a
  double-submit can't double-count a question or trigger a false core override.
- Reject incomplete submissions (every quiz question must be answered),
  unknown questions, and invalid options with HTTP 400.
- Derive each answer's ``is_core`` flag from the configured core questions
  rather than a hardcoded [1, 3, 5].
"""

from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from ..api.models import ImageAnswer, QuizAnswer
from .quiz_scoring import QuizAnswer as ScoringQuizAnswer


def _question_id(raw: Any) -> Optional[int]:
    """Coerce a question id to int (DynamoDB returns numbers as Decimal)."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _extract_answer_text(answer: QuizAnswer) -> str:
    """Return the selected option's display text for any answer shape."""
    value = answer.answer
    if isinstance(value, ImageAnswer):
        return value.label
    if isinstance(value, dict):
        return value.get("label") or value.get("text") or ""
    return str(value)


def _dedupe_keep_last(answers: List[QuizAnswer]) -> List[QuizAnswer]:
    """Keep one answer per question id (the last submitted), in id order.

    A well-behaved client sends a single answer per question. Duplicates — seen
    in production from rapid re-submits — would otherwise be scored multiple
    times. The latest answer is treated as the user's final choice.
    """
    by_qid: Dict[int, QuizAnswer] = {}
    for answer in answers:
        by_qid[answer.questionId] = answer
    return [by_qid[qid] for qid in sorted(by_qid)]


def build_scoring_inputs(
    answers: List[QuizAnswer],
    questions_list: List[Dict[str, Any]],
    core_question_ids: List[int],
) -> Tuple[List[ScoringQuizAnswer], List[Dict[str, Any]]]:
    """Validate answers and map them to archetypes.

    Args:
        answers: Raw submitted answers.
        questions_list: Quiz questions (id, options, ...) from DynamoDB/JSON.
        core_question_ids: Ids of the core (higher-weight) questions.

    Returns:
        (scoring_answers, storage_answers).

    Raises:
        HTTPException: 400 for incomplete submissions, unknown questions, or
            invalid options.
    """
    # question_id -> {option_text: archetype}
    question_map: Dict[int, Dict[str, str]] = {}
    for question in questions_list:
        q_id = _question_id(question.get("id"))
        if q_id is None:
            continue
        question_map[q_id] = {
            (opt.get("text") or opt.get("label")): opt.get("archetype")
            for opt in question.get("options", [])
        }

    deduped = _dedupe_keep_last(answers)
    answered_ids = {a.questionId for a in deduped}

    # Completeness: every quiz question must be answered (spec V1).
    required_ids = set(question_map)
    missing = required_ids - answered_ids
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                "Incomplete submission: missing answers for "
                f"questions {sorted(missing)}"
            ),
        )

    core_set = set(core_question_ids)
    scoring_answers: List[ScoringQuizAnswer] = []
    storage_answers: List[Dict[str, Any]] = []

    for answer in deduped:
        q_id = answer.questionId
        if q_id not in question_map:
            raise HTTPException(
                status_code=400, detail=f"Question {q_id} not found in quiz data"
            )

        answer_text = _extract_answer_text(answer)
        archetype = question_map[q_id].get(answer_text)
        if not archetype:
            raise HTTPException(
                status_code=400,
                detail=f"Answer '{answer_text}' not valid for question {q_id}",
            )

        scoring_answers.append(
            ScoringQuizAnswer(
                question_id=q_id,
                question=answer.question,
                archetype=archetype,
                is_core=q_id in core_set,
            )
        )
        storage_answers.append(
            {
                "question_id": q_id,
                "question": answer.question,
                "answer": answer_text,
                "archetype": archetype,
                "answered_at": answer.answeredAt,
                "type": answer.type,
            }
        )

    return scoring_answers, storage_answers
