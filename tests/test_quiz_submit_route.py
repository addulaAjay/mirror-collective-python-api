"""Route-level guards for POST /api/mirrorgpt/quiz/submit (hardening bundle).

Exercises the endpoint end-to-end (falling back to the bundled questions.json):
  #9 an incomplete submission is rejected with 400
  #7 duplicate answers for a question are scored once (last wins)
  #5 the full 5-answer happy path still scores correctly through the config path
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.app.api.mirrorgpt_routes import get_dynamodb_service, get_mirror_orchestrator
from src.app.handler import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def overrides():
    db = AsyncMock()
    db.get_quiz_questions = AsyncMock(return_value=None)  # -> questions.json fallback
    orch = AsyncMock()
    orch.create_initial_archetype_profile = AsyncMock(
        return_value={"success": True, "profile_created": True}
    )
    app.dependency_overrides[get_dynamodb_service] = lambda: db
    app.dependency_overrides[get_mirror_orchestrator] = lambda: orch
    try:
        yield orch
    finally:
        app.dependency_overrides.clear()


def _answer(qid, text, at_suffix="00"):
    return {
        "questionId": qid,
        "question": f"Q{qid}",
        "answer": text,
        "answeredAt": f"2026-04-19T10:00:{at_suffix}Z",
        "type": "text" if qid != 2 else "image",
    }


# Canonical answers that map to Seeker on the bundled questions.json.
SEEKER_ANSWERS = [
    _answer(1, "A quiet aha when something finally clicks", "00"),
    _answer(2, "A glowing crystal sphere", "10"),
    _answer(3, "I want to understand myself better.", "20"),
    _answer(4, "I give them clarity or insight they didn't see before.", "30"),
    _answer(5, "I'm here to learn and grow.", "40"),
]


def _request(answers):
    return {
        "quiz_type": "archetype",
        "answers": answers,
        "completedAt": "2026-04-19T10:00:40Z",
        "quizVersion": "1.0",
        "anonymousId": "test123",
    }


def test_incomplete_submission_returns_400(client, overrides):
    # Missing Q5 -> reject (#9)
    resp = client.post("/api/mirrorgpt/quiz/submit", json=_request(SEEKER_ANSWERS[:4]))
    assert resp.status_code == 400
    assert "missing" in resp.text.lower()


def test_full_submission_scores_seeker(client, overrides):
    # #5: full set scores correctly via the config-driven path.
    resp = client.post("/api/mirrorgpt/quiz/submit", json=_request(SEEKER_ANSWERS))
    assert resp.status_code == 200
    assert resp.json()["data"]["final_archetype"] == "Seeker"


def test_duplicate_answer_scored_once(client, overrides):
    # #7: a duplicate Q5 (Guardian then Seeker) must not double-count; the
    # last answer wins and the result stays a clean Seeker core override.
    answers = [
        SEEKER_ANSWERS[0],
        SEEKER_ANSWERS[1],
        SEEKER_ANSWERS[2],
        SEEKER_ANSWERS[3],
        _answer(5, "I'm here to care and connect.", "40"),  # Guardian (superseded)
        _answer(5, "I'm here to learn and grow.", "45"),  # Seeker (final)
    ]
    resp = client.post("/api/mirrorgpt/quiz/submit", json=_request(answers))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["final_archetype"] == "Seeker"
    # The superseded Guardian answer for Q5 must NOT be counted — without dedup
    # it would add 2 points to Guardian. Seeker stays 8 (all 5 answers Seeker).
    assert data["total_scores"]["Guardian"] == 0
    assert data["total_scores"]["Seeker"] == 8
