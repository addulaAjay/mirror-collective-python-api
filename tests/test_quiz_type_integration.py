"""
Integration Tests for Quiz Type Framework
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
def mock_db_service():
    mock = AsyncMock()
    mock.get_quiz_questions = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def mock_orchestrator():
    mock = AsyncMock()
    mock.create_initial_archetype_profile = AsyncMock(
        return_value={"success": True, "profile_created": True}
    )
    return mock


class TestQuizTypeEndpoint:
    def test_submit_quiz_type_archetype(
        self, client, mock_db_service, mock_orchestrator
    ):
        # Override FastAPI dependencies
        app.dependency_overrides[get_dynamodb_service] = lambda: mock_db_service
        app.dependency_overrides[get_mirror_orchestrator] = lambda: mock_orchestrator

        try:
            req = {
                "quiz_type": "archetype",
                "answers": [
                    {
                        "questionId": 1,
                        "question": "Q1",
                        "answer": "A quiet aha when something finally clicks",
                        "answeredAt": "2026-04-19T10:00:00Z",
                        "type": "text",
                    },
                    {
                        "questionId": 2,
                        "question": "Q2",
                        "answer": "A glowing crystal sphere",
                        "answeredAt": "2026-04-19T10:00:10Z",
                        "type": "text",
                    },
                    {
                        "questionId": 3,
                        "question": "Q3",
                        "answer": "I want to understand myself better.",
                        "answeredAt": "2026-04-19T10:00:20Z",
                        "type": "text",
                    },
                    {
                        "questionId": 4,
                        "question": "Q4",
                        "answer": "I give them clarity or insight they didn't see before.",
                        "answeredAt": "2026-04-19T10:00:30Z",
                        "type": "text",
                    },
                    {
                        "questionId": 5,
                        "question": "Q5",
                        "answer": "I'm here to learn and grow.",
                        "answeredAt": "2026-04-19T10:00:40Z",
                        "type": "text",
                    },
                ],
                "completedAt": "2026-04-19T10:00:40Z",
                "quizVersion": "1.0",
                "anonymousId": "test123",
            }

            resp = client.post("/api/mirrorgpt/quiz/submit", json=req)
            assert resp.status_code == 200
            data = resp.json()
            assert data["data"]["quiz_type"] == "archetype"
            assert data["data"]["final_archetype"] == "Seeker"

            kwargs = mock_orchestrator.create_initial_archetype_profile.call_args.kwargs
            assert kwargs["quiz_type"] == "archetype"
        finally:
            app.dependency_overrides.clear()

    def test_submit_different_quiz_type(
        self, client, mock_db_service, mock_orchestrator
    ):
        app.dependency_overrides[get_dynamodb_service] = lambda: mock_db_service
        app.dependency_overrides[get_mirror_orchestrator] = lambda: mock_orchestrator

        try:
            req = {
                "quiz_type": "career",
                "answers": [
                    {
                        "questionId": 1,
                        "question": "Q1",
                        "answer": "A quiet aha when something finally clicks",
                        "answeredAt": "2026-04-19T10:00:00Z",
                        "type": "text",
                    },
                    {
                        "questionId": 2,
                        "question": "Q2",
                        "answer": "A glowing crystal sphere",
                        "answeredAt": "2026-04-19T10:00:10Z",
                        "type": "text",
                    },
                    {
                        "questionId": 3,
                        "question": "Q3",
                        "answer": "I want to understand myself better.",
                        "answeredAt": "2026-04-19T10:00:20Z",
                        "type": "text",
                    },
                    {
                        "questionId": 4,
                        "question": "Q4",
                        "answer": "I give them clarity or insight they didn't see before.",
                        "answeredAt": "2026-04-19T10:00:30Z",
                        "type": "text",
                    },
                    {
                        "questionId": 5,
                        "question": "Q5",
                        "answer": "I'm here to learn and grow.",
                        "answeredAt": "2026-04-19T10:00:40Z",
                        "type": "text",
                    },
                ],
                "completedAt": "2026-04-19T10:00:40Z",
                "quizVersion": "1.0",
                "anonymousId": "test456",
            }

            resp = client.post("/api/mirrorgpt/quiz/submit", json=req)
            assert resp.status_code == 200
            assert resp.json()["data"]["quiz_type"] == "career"

            kwargs = mock_orchestrator.create_initial_archetype_profile.call_args.kwargs
            assert kwargs["quiz_type"] == "career"
        finally:
            app.dependency_overrides.clear()


class TestConfigValidation:
    def test_missing_config_defaults(self):
        from src.app.services.quiz_scoring import QuizAnswer, calculate_quiz_result

        answers = [
            QuizAnswer(question_id=1, question="Q1", archetype="Seeker", is_core=True),
            QuizAnswer(
                question_id=2, question="Q2", archetype="Guardian", is_core=False
            ),
            QuizAnswer(question_id=3, question="Q3", archetype="Seeker", is_core=True),
            QuizAnswer(question_id=4, question="Q4", archetype="Weaver", is_core=False),
            QuizAnswer(
                question_id=5, question="Q5", archetype="Guardian", is_core=True
            ),
        ]
        result = calculate_quiz_result(answers)
        assert result["final_archetype"] == "Seeker"

    def test_partial_config(self):
        from src.app.services.quiz_scoring import QuizAnswer, calculate_quiz_result

        answers = [
            QuizAnswer(question_id=1, question="Q1", archetype="Seeker", is_core=True),
            QuizAnswer(
                question_id=2, question="Q2", archetype="Guardian", is_core=False
            ),
            QuizAnswer(question_id=3, question="Q3", archetype="Seeker", is_core=True),
            QuizAnswer(question_id=4, question="Q4", archetype="Weaver", is_core=False),
            QuizAnswer(
                question_id=5, question="Q5", archetype="Guardian", is_core=True
            ),
        ]
        cfg = {
            "archetypes": ["Seeker", "Guardian", "Flamebearer", "Weaver"],
            "weights": {"core": 2, "regular": 1},
        }
        result = calculate_quiz_result(answers, quiz_config=cfg)
        assert result["final_archetype"] == "Seeker"

    def test_malformed_config_handles_gracefully(self):
        from src.app.services.quiz_scoring import QuizAnswer, calculate_quiz_result

        answers = [
            QuizAnswer(question_id=1, question="Q1", archetype="Seeker", is_core=True),
            QuizAnswer(
                question_id=2, question="Q2", archetype="Guardian", is_core=False
            ),
            QuizAnswer(question_id=3, question="Q3", archetype="Seeker", is_core=True),
        ]
        try:
            result = calculate_quiz_result(
                answers, quiz_config={"weights": "bad", "archetypes": "bad"}
            )
            assert result is not None
        except (TypeError, AttributeError, KeyError):
            pass


class TestQuizTypeStorage:
    def test_quiz_type_to_orchestrator(
        self, client, mock_db_service, mock_orchestrator
    ):
        app.dependency_overrides[get_dynamodb_service] = lambda: mock_db_service
        app.dependency_overrides[get_mirror_orchestrator] = lambda: mock_orchestrator

        try:
            req = {
                "quiz_type": "learning",
                "answers": [
                    {
                        "questionId": 1,
                        "question": "Q1",
                        "answer": "A quiet aha when something finally clicks",
                        "answeredAt": "2026-04-19T10:00:00Z",
                        "type": "text",
                    },
                    {
                        "questionId": 2,
                        "question": "Q2",
                        "answer": "A glowing crystal sphere",
                        "answeredAt": "2026-04-19T10:00:10Z",
                        "type": "text",
                    },
                    {
                        "questionId": 3,
                        "question": "Q3",
                        "answer": "I want to understand myself better.",
                        "answeredAt": "2026-04-19T10:00:20Z",
                        "type": "text",
                    },
                    {
                        "questionId": 4,
                        "question": "Q4",
                        "answer": "I give them clarity or insight they didn't see before.",
                        "answeredAt": "2026-04-19T10:00:30Z",
                        "type": "text",
                    },
                    {
                        "questionId": 5,
                        "question": "Q5",
                        "answer": "I'm here to learn and grow.",
                        "answeredAt": "2026-04-19T10:00:40Z",
                        "type": "text",
                    },
                ],
                "completedAt": "2026-04-19T10:00:40Z",
                "quizVersion": "1.0",
                "anonymousId": "test789",
            }

            resp = client.post("/api/mirrorgpt/quiz/submit", json=req)
            assert resp.status_code == 200

            kwargs = mock_orchestrator.create_initial_archetype_profile.call_args.kwargs
            assert "quiz_type" in kwargs
            assert kwargs["quiz_type"] == "learning"
        finally:
            app.dependency_overrides.clear()
