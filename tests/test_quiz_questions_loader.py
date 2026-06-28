"""Tests for the bundled quiz-questions loader and the GET /quiz/questions route.

The quiz is served from src/app/data/questions.json (baked into the deploy) to
avoid a DynamoDB scan on the request path. These tests pin the bundled file's
integrity and that the endpoint serves it without any DynamoDB dependency.
"""

import os

import pytest
from fastapi.testclient import TestClient

from src.app.handler import app
from src.app.services import quiz_questions_loader as loader

VALID_ARCHETYPES = {"Seeker", "Guardian", "Flamebearer", "Weaver"}


@pytest.fixture(autouse=True)
def _reset_loader_cache():
    loader.reset_cache()
    yield
    loader.reset_cache()


def test_loader_returns_five_questions():
    questions = loader.get_quiz_questions()
    assert len(questions) == 5
    assert sorted(q["id"] for q in questions) == [1, 2, 3, 4, 5]


def test_loader_is_cached():
    assert loader.load_quiz_data() is loader.load_quiz_data()


def test_every_option_maps_to_a_valid_archetype():
    for q in loader.get_quiz_questions():
        opts = q["options"]
        assert len(opts) == 4
        for opt in opts:
            assert opt["archetype"] in VALID_ARCHETYPES
            # Each option must carry display text (text) or image label (label).
            assert opt.get("text") or opt.get("label")


def test_core_questions_are_1_3_5():
    core = sorted(q["id"] for q in loader.get_quiz_questions() if q.get("core"))
    assert core == [1, 3, 5]


def test_config_and_archetypes_present():
    data = loader.load_quiz_data()
    assert set(data["config"]["archetypes"]) == VALID_ARCHETYPES
    assert set(data["archetypes"].keys()) == {a.lower() for a in VALID_ARCHETYPES}


def test_get_quiz_questions_route_serves_bundled_no_dynamodb():
    client = TestClient(app)
    resp = client.get("/api/mirrorgpt/quiz/questions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert len(body["data"]) == 5
    assert body["data"] == loader.get_quiz_questions()


@pytest.mark.skipif(
    not os.getenv("AWS_ACCESS_KEY_ID") and not os.getenv("AWS_PROFILE"),
    reason="no AWS credentials; live-sync check is integration-only",
)
def test_bundled_file_in_sync_with_live_table():
    """Integration: the bundled questions must match the DynamoDB table.

    Mirrors `scripts/export_quiz_questions.py --check`. Skipped without creds.
    """
    import importlib.util
    from pathlib import Path

    script = (
        Path(__file__).resolve().parent.parent / "scripts" / "export_quiz_questions.py"
    )
    spec = importlib.util.spec_from_file_location("export_quiz_questions", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    table_questions = mod.fetch_table_questions(mod.TABLE_NAME)
    assert loader.get_quiz_questions() == table_questions
