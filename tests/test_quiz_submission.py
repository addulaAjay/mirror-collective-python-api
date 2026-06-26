"""Unit tests for quiz answer mapping/validation (quiz-scoring hardening).

Covers:
  #6 core flag derived from the configured core questions (not hardcoded 1/3/5)
  #7 duplicate answers per question are de-duplicated (keep last)
  #8 robust answer-text extraction for text, ImageAnswer model, and dict
  #9 incomplete submissions (a quiz question unanswered) are rejected
"""

import pytest
from fastapi import HTTPException

from src.app.api.models import ImageAnswer, QuizAnswer
from src.app.services.quiz_submission import (
    _dedupe_keep_last,
    _extract_answer_text,
    build_scoring_inputs,
)

QUESTIONS = [
    {
        "id": 1,
        "question": "Q1",
        "core": True,
        "type": "text",
        "options": [
            {"text": "aha", "archetype": "Seeker"},
            {"text": "care", "archetype": "Guardian"},
        ],
    },
    {
        "id": 2,
        "question": "Q2",
        "core": False,
        "type": "image",
        "options": [
            {"label": "crystal", "image": "crystal", "archetype": "Seeker"},
            {"label": "candle", "image": "candle", "archetype": "Guardian"},
        ],
    },
    {
        "id": 3,
        "question": "Q3",
        "core": True,
        "type": "text",
        "options": [
            {"text": "grow", "archetype": "Seeker"},
            {"text": "connect", "archetype": "Guardian"},
        ],
    },
]

CORE_IDS = [1, 3]


def _ans(qid, answer, type_="text", at="2026-01-01T00:00:00Z"):
    return QuizAnswer(
        questionId=qid, question=f"Q{qid}", answer=answer, answeredAt=at, type=type_
    )


def _full_text_answers():
    return [_ans(1, "aha"), _ans(2, "crystal", "image"), _ans(3, "grow")]


def test_happy_path_maps_archetypes_and_core_flag():
    scoring, storage = build_scoring_inputs(_full_text_answers(), QUESTIONS, CORE_IDS)

    assert [s["archetype"] for s in scoring] == ["Seeker", "Seeker", "Seeker"]
    # #6: is_core derived from CORE_IDS, not a hardcoded [1,3,5]
    assert {s["question_id"]: s["is_core"] for s in scoring} == {
        1: True,
        2: False,
        3: True,
    }
    assert len(storage) == 3
    assert storage[0]["answer"] == "aha"
    assert storage[1]["archetype"] == "Seeker"


def test_core_flag_follows_configured_core_questions():
    # #6: a quiz whose core questions are [2] must mark q2 core, not q1/q3.
    scoring, _ = build_scoring_inputs(_full_text_answers(), QUESTIONS, [2])
    assert {s["question_id"]: s["is_core"] for s in scoring} == {
        1: False,
        2: True,
        3: False,
    }


def test_image_answer_model_extracted_by_label():
    # #8: ImageAnswer is a pydantic model, not a dict — must still map by label.
    answers = [
        _ans(1, "aha"),
        _ans(2, ImageAnswer(label="candle", image="candle"), "image"),
        _ans(3, "grow"),
    ]
    scoring, _ = build_scoring_inputs(answers, QUESTIONS, CORE_IDS)
    by_id = {s["question_id"]: s["archetype"] for s in scoring}
    assert by_id[2] == "Guardian"


def test_image_answer_dict_extracted_by_label():
    assert (
        _extract_answer_text(_ans(2, {"label": "candle", "image": "c"}, "image"))
        == "candle"
    )


def test_extract_text_variants():
    assert _extract_answer_text(_ans(1, "aha")) == "aha"
    assert (
        _extract_answer_text(_ans(2, ImageAnswer(label="crystal", image="c")))
        == "crystal"
    )


def test_duplicate_answers_keep_last():
    # #7: two answers for q3 -> last one wins, counted once.
    answers = [
        _ans(1, "aha"),
        _ans(2, "crystal", "image"),
        _ans(3, "grow"),
        _ans(3, "connect"),  # user changed their mind / double submit
    ]
    deduped = _dedupe_keep_last(answers)
    assert [a.questionId for a in deduped] == [1, 2, 3]

    scoring, _ = build_scoring_inputs(answers, QUESTIONS, CORE_IDS)
    q3 = next(s for s in scoring if s["question_id"] == 3)
    assert q3["archetype"] == "Guardian"  # last answer ("connect")
    assert len([s for s in scoring if s["question_id"] == 3]) == 1


def test_incomplete_submission_rejected():
    # #9: missing q2 -> 400
    answers = [_ans(1, "aha"), _ans(3, "grow")]
    with pytest.raises(HTTPException) as exc:
        build_scoring_inputs(answers, QUESTIONS, CORE_IDS)
    assert exc.value.status_code == 400
    assert "2" in str(exc.value.detail)


def test_unknown_question_rejected():
    answers = _full_text_answers() + [_ans(99, "aha")]
    with pytest.raises(HTTPException) as exc:
        build_scoring_inputs(answers, QUESTIONS, CORE_IDS)
    assert exc.value.status_code == 400


def test_invalid_option_rejected():
    answers = [_ans(1, "not-an-option"), _ans(2, "crystal", "image"), _ans(3, "grow")]
    with pytest.raises(HTTPException) as exc:
        build_scoring_inputs(answers, QUESTIONS, CORE_IDS)
    assert exc.value.status_code == 400
    assert "not valid" in str(exc.value.detail)
