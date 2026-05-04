"""Integration tests for POST /practice/complete + PATCH .../helpful (spec §B.3.4)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from src.app.api.practice_routes import (
    get_echo_loop_state_repo,
    get_practice_completion_repo,
    get_reflection_session_repo,
    get_telemetry_emitter,
    get_user_personalization_repo,
)
from src.app.core.security import get_current_user
from src.app.handler import app
from src.app.models.echo_loop_state import EchoLoopState
from src.app.models.reflection_session import ReflectionSession
from src.app.repositories.echo_loop_state_repo import EchoLoopStateRepo
from src.app.repositories.practice_completion_repo import PracticeCompletionRepo
from src.app.repositories.reflection_session_repo import (
    GSI_USER_CREATED,
    ReflectionSessionRepo,
)
from src.app.repositories.user_personalization_repo import UserPersonalizationRepo
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

SESSIONS = "mc_reflection_sessions-test"
LOOPS = "mc_echo_loop_state-test"
COMPLETIONS = "mc_practice_completions-test"
PREFS = "mc_user_personalization-test"


async def _fake_user() -> Dict[str, Any]:
    return {"id": "test-user-123", "sub": "test-user-123", "email": "test@example.com"}


class _SpyEmitter:
    """Captures emitted events for assertion."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def emit(self, event_name: str, *, user_hash: str, **fields: Any) -> None:
        self.events.append({"event": event_name, "user_hash": user_hash, **fields})


@pytest.fixture
def fake_tables() -> Dict[str, FakeTable]:
    return {
        SESSIONS: FakeTable(
            primary_key=["session_id"],
            indexes={GSI_USER_CREATED: ["user_id", "created_at"]},
        ),
        LOOPS: FakeTable(primary_key=["user_id", "loop_id"]),
        COMPLETIONS: FakeTable(
            primary_key=["user_id", "completion_id"],
            indexes={"practice_id-completed_at-index": ["practice_id", "completed_at"]},
        ),
        PREFS: FakeTable(primary_key=["user_id"]),
    }


@pytest.fixture
def repos(fake_tables: Dict[str, FakeTable], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DYNAMODB_REFLECTION_SESSIONS_TABLE", SESSIONS)
    monkeypatch.setenv("DYNAMODB_ECHO_LOOP_STATE_TABLE", LOOPS)
    monkeypatch.setenv("DYNAMODB_PRACTICE_COMPLETIONS_TABLE", COMPLETIONS)
    monkeypatch.setenv("DYNAMODB_USER_PERSONALIZATION_TABLE", PREFS)
    sess = FakeAioSession(fake_tables)
    return {
        "sessions": ReflectionSessionRepo(session=sess),
        "loop_states": EchoLoopStateRepo(session=sess),
        "completions": PracticeCompletionRepo(session=sess),
        "prefs": UserPersonalizationRepo(session=sess),
    }


@pytest.fixture
def emitter() -> _SpyEmitter:
    return _SpyEmitter()


@pytest.fixture
def client(repos, emitter) -> Iterator[TestClient]:
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_reflection_session_repo] = lambda: repos["sessions"]
    app.dependency_overrides[get_echo_loop_state_repo] = lambda: repos["loop_states"]
    app.dependency_overrides[get_practice_completion_repo] = lambda: repos[
        "completions"
    ]
    app.dependency_overrides[get_user_personalization_repo] = lambda: repos["prefs"]
    app.dependency_overrides[get_telemetry_emitter] = lambda: emitter
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (
            get_reflection_session_repo,
            get_echo_loop_state_repo,
            get_practice_completion_repo,
            get_user_personalization_repo,
            get_telemetry_emitter,
        ):
            app.dependency_overrides.pop(dep, None)


def _seed_session(repos, **overrides) -> ReflectionSession:
    base = dict(
        user_id="test-user-123",
        motif_id="spiral",
        motif_name="Spiral",
        room_skin="Spiral Room",
        motif_payload={"override_allowed": False},
        quiz_answers={
            "q1": "hopeful",
            "q2": "inspiration",
            "q3": "spiral",
            "q4": "insight",
        },
        scores={"evolution": 5},
        user_tz="America/New_York",
        expires_at="2099-01-01T00:00:00Z",
        created_at="2026-05-03T10:00:00Z",
    )
    base.update(overrides)
    s = ReflectionSession(**base)
    asyncio.run(repos["sessions"].put(s))
    return s


def _seed_loop(repos, **overrides) -> EchoLoopState:
    base = dict(
        user_id="test-user-123",
        loop_id="pressure",
        tone_state="rising",
        intensity_score=0.74,
        intensity_label="High",
    )
    base.update(overrides)
    state = EchoLoopState(**base)
    asyncio.run(repos["loop_states"].upsert(state))
    return state


# ============================================================
# POST /practice/complete
# ============================================================


class TestCompletePractice:
    def test_happy_path_helpful_true(self, client, repos, emitter):
        session = _seed_session(repos)
        _seed_loop(repos, loop_id="pressure", intensity_score=0.74)

        body = {
            "session_id": session.session_id,
            "loop_id": "pressure",
            "tone_state": "rising",
            "practice_id": "breath_4_6",
            "rule_id": "pressure_loop_v1",
            "helpful": True,
        }
        response = client.post("/api/practice/complete", json=body)
        assert response.status_code == 200
        data = response.json()["data"]
        assert "completion_id" in data
        # Snapshot in response reflects state delta.
        assert "snapshot" in data
        snapshot_loops = data["snapshot"]["loops"]
        # pressure intensity reduced 0.74 → 0.64; tone flipped to softening.
        pressure = next(l for l in snapshot_loops if l["loop_id"] == "pressure")
        assert pressure["intensity_score"] == pytest.approx(0.64, abs=1e-3)
        assert pressure["tone_state"] == "softening"

    def test_personalizer_records_helpfulness(self, client, repos):
        session = _seed_session(repos)
        _seed_loop(repos, loop_id="pressure", intensity_score=0.74)
        client.post(
            "/api/practice/complete",
            json={
                "session_id": session.session_id,
                "loop_id": "pressure",
                "tone_state": "rising",
                "practice_id": "breath_4_6",
                "rule_id": "pressure_loop_v1",
                "helpful": True,
            },
        )
        prefs = asyncio.run(repos["prefs"].get_or_default("test-user-123"))
        events = prefs.practice_helpfulness.get("breath_4_6", [])
        assert len(events) == 1
        assert events[0].helpful is True
        # recent_use updated too.
        assert "breath_4_6" in prefs.recent_use
        assert prefs.recent_use["breath_4_6"].count_30d == 1

    def test_null_helpful_records_completion_only(self, client, repos):
        session = _seed_session(repos)
        _seed_loop(repos, loop_id="pressure", intensity_score=0.74)
        response = client.post(
            "/api/practice/complete",
            json={
                "session_id": session.session_id,
                "loop_id": "pressure",
                "tone_state": "rising",
                "practice_id": "breath_4_6",
                "rule_id": "pressure_loop_v1",
                # no helpful field
            },
        )
        assert response.status_code == 200
        # Loop state untouched.
        loop = asyncio.run(repos["loop_states"].get("test-user-123", "pressure"))
        assert loop.intensity_score == 0.74
        assert loop.tone_state == "rising"
        # No helpfulness event recorded.
        prefs = asyncio.run(repos["prefs"].get_or_default("test-user-123"))
        assert prefs.practice_helpfulness.get("breath_4_6", []) == []
        # recent_use still updated.
        assert "breath_4_6" in prefs.recent_use

    def test_emits_practice_complete_and_helpful_events(self, client, repos, emitter):
        session = _seed_session(repos)
        _seed_loop(repos, loop_id="pressure", intensity_score=0.74)
        client.post(
            "/api/practice/complete",
            json={
                "session_id": session.session_id,
                "loop_id": "pressure",
                "tone_state": "rising",
                "practice_id": "breath_4_6",
                "rule_id": "pressure_loop_v1",
                "helpful": True,
            },
        )
        names = [e["event"] for e in emitter.events]
        assert "practice_complete" in names
        assert "practice_helpful" in names
        # Verify enums-only payload (no free text).
        for ev in emitter.events:
            for k, v in ev.items():
                assert isinstance(v, (str, int, float, bool)), f"{k}={v!r}"

    def test_emits_practice_not_helpful_when_false(self, client, repos, emitter):
        session = _seed_session(repos)
        _seed_loop(repos, loop_id="pressure", intensity_score=0.74)
        client.post(
            "/api/practice/complete",
            json={
                "session_id": session.session_id,
                "loop_id": "pressure",
                "tone_state": "rising",
                "practice_id": "breath_4_6",
                "rule_id": "pressure_loop_v1",
                "helpful": False,
            },
        )
        names = [e["event"] for e in emitter.events]
        assert "practice_not_helpful" in names
        assert "practice_helpful" not in names

    def test_invalid_loop_id_returns_422(self, client, repos):
        session = _seed_session(repos)
        response = client.post(
            "/api/practice/complete",
            json={
                "session_id": session.session_id,
                "loop_id": "clarity",  # not V1
                "tone_state": "rising",
                "practice_id": "breath_4_6",
                "rule_id": "pressure_loop_v1",
            },
        )
        assert response.status_code == 422

    def test_unknown_session_id_returns_404(self, client, repos):
        response = client.post(
            "/api/practice/complete",
            json={
                "session_id": "does-not-exist",
                "loop_id": "pressure",
                "tone_state": "rising",
                "practice_id": "breath_4_6",
                "rule_id": "pressure_loop_v1",
            },
        )
        assert response.status_code == 404


# ============================================================
# PATCH /practice/complete/{completion_id}/helpful
# ============================================================


class TestUpdateHelpful:
    def test_late_vote_updates_completion_and_state(self, client, repos):
        session = _seed_session(repos)
        _seed_loop(repos, loop_id="pressure", intensity_score=0.74)

        # First POST without helpful.
        post_response = client.post(
            "/api/practice/complete",
            json={
                "session_id": session.session_id,
                "loop_id": "pressure",
                "tone_state": "rising",
                "practice_id": "breath_4_6",
                "rule_id": "pressure_loop_v1",
            },
        )
        completion_id = post_response.json()["data"]["completion_id"]

        # State unchanged.
        loop = asyncio.run(repos["loop_states"].get("test-user-123", "pressure"))
        assert loop.intensity_score == 0.74

        # Late helpful vote. completion_id contains '#' → URL-encode the
        # path segment so it doesn't get parsed as a fragment delimiter.
        # Frontend clients must encode here too — documented for FE handoff.
        patch_response = client.patch(
            f"/api/practice/complete/{quote(completion_id, safe='')}/helpful",
            json={"helpful": True},
        )
        assert patch_response.status_code == 200
        # State now reflects the drop.
        loop = asyncio.run(repos["loop_states"].get("test-user-123", "pressure"))
        assert loop.intensity_score == pytest.approx(0.64, abs=1e-3)
        assert loop.tone_state == "softening"

    def test_unknown_completion_id_returns_404(self, client):
        response = client.patch(
            f"/api/practice/complete/{quote('does-not-exist', safe='')}/helpful",
            json={"helpful": True},
        )
        assert response.status_code == 404
