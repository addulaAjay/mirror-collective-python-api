"""Integration tests for GET /echo/snapshot + POST /dev/echo/loop-state."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient

from src.app.api.echo_v1_routes import (
    get_echo_loop_state_repo,
    get_practice_completion_repo,
    get_reflection_session_repo,
    get_telemetry_emitter,
    get_user_personalization_repo,
)
from src.app.core.security import get_current_user
from src.app.handler import app
from src.app.models.echo_loop_state import EchoLoopState
from src.app.models.practice_completion import PracticeCompletion
from src.app.models.reflection_session import ReflectionSession
from src.app.repositories.echo_loop_state_repo import EchoLoopStateRepo
from src.app.repositories.practice_completion_repo import PracticeCompletionRepo
from src.app.repositories.reflection_session_repo import (
    GSI_USER_CREATED,
    ReflectionSessionRepo,
)
from src.app.repositories.user_personalization_repo import UserPersonalizationRepo
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

SESSIONS_TABLE = "mc_reflection_sessions-test"
LOOP_STATE_TABLE = "mc_echo_loop_state-test"
COMPLETIONS_TABLE = "mc_practice_completions-test"
PREFS_TABLE = "mc_user_personalization-test"


async def _fake_user_no_varargs() -> Dict[str, Any]:
    return {
        "id": "test-user-123",
        "sub": "test-user-123",
        "email": "test@example.com",
    }


@pytest.fixture
def fake_tables() -> Dict[str, FakeTable]:
    return {
        SESSIONS_TABLE: FakeTable(
            primary_key=["session_id"],
            indexes={GSI_USER_CREATED: ["user_id", "created_at"]},
        ),
        LOOP_STATE_TABLE: FakeTable(primary_key=["user_id", "loop_id"]),
        COMPLETIONS_TABLE: FakeTable(
            primary_key=["user_id", "completion_id"],
            indexes={"practice_id-completed_at-index": ["practice_id", "completed_at"]},
        ),
        PREFS_TABLE: FakeTable(primary_key=["user_id"]),
    }


@pytest.fixture
def repos(fake_tables: Dict[str, FakeTable], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DYNAMODB_REFLECTION_SESSIONS_TABLE", SESSIONS_TABLE)
    monkeypatch.setenv("DYNAMODB_ECHO_LOOP_STATE_TABLE", LOOP_STATE_TABLE)
    monkeypatch.setenv("DYNAMODB_PRACTICE_COMPLETIONS_TABLE", COMPLETIONS_TABLE)
    monkeypatch.setenv("DYNAMODB_USER_PERSONALIZATION_TABLE", PREFS_TABLE)
    sess = FakeAioSession(fake_tables)
    return {
        "sessions": ReflectionSessionRepo(session=sess),
        "loop_states": EchoLoopStateRepo(session=sess),
        "completions": PracticeCompletionRepo(session=sess),
        "prefs": UserPersonalizationRepo(session=sess),
    }


class _SpyEmitter:
    """Captures emitted events for assertion."""

    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event_name: str, *, user_hash: str, **fields) -> None:
        self.events.append({"event": event_name, "user_hash": user_hash, **fields})


@pytest.fixture
def emitter() -> _SpyEmitter:
    return _SpyEmitter()


@pytest.fixture
def client(repos: Dict[str, Any], emitter: _SpyEmitter) -> Iterator[TestClient]:
    app.dependency_overrides[get_current_user] = _fake_user_no_varargs
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
        app.dependency_overrides.pop(get_reflection_session_repo, None)
        app.dependency_overrides.pop(get_echo_loop_state_repo, None)
        app.dependency_overrides.pop(get_practice_completion_repo, None)
        app.dependency_overrides.pop(get_user_personalization_repo, None)
        app.dependency_overrides.pop(get_telemetry_emitter, None)


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
    session = ReflectionSession(**base)
    asyncio.run(repos["sessions"].put(session))
    return session


def _seed_loop(repos, **overrides) -> EchoLoopState:
    base = dict(
        user_id="test-user-123",
        loop_id="pressure",
        tone_state="rising",
        intensity_score=0.74,
        intensity_label="High",
        last_seen="2026-05-03T20:10:00Z",
    )
    base.update(overrides)
    state = EchoLoopState(**base)
    asyncio.run(repos["loop_states"].upsert(state))
    return state


# ============================================================
# GET /echo/snapshot
# ============================================================


class TestSnapshotEndpoint:
    def test_returns_200_with_loops_sorted(self, client: TestClient, repos):
        _seed_session(repos)
        _seed_loop(repos, loop_id="pressure", intensity_score=0.74)
        _seed_loop(repos, loop_id="grief", intensity_score=0.58, tone_state="softening")

        response = client.get("/api/echo/snapshot")
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        data = body["data"]
        assert data["motif_context"]["motif_id"] == "spiral"
        assert data["motif_context"]["room_skin"] == "Spiral Room"
        loop_ids = [l["loop_id"] for l in data["loops"]]
        assert loop_ids == ["pressure", "grief"]
        assert data["loops"][0]["icon"] == "🔺"
        assert "reflection_line" in data["loops"][0]

    def test_empty_state_returns_200_with_no_loops(self, client: TestClient, repos):
        _seed_session(repos)
        response = client.get("/api/echo/snapshot")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["loops"] == []
        assert data["motif_context"]["motif_id"] == "spiral"

    def test_invalid_session_id_returns_404(self, client: TestClient, repos):
        _seed_session(repos)
        response = client.get(
            "/api/echo/snapshot", params={"session_id": "does-not-exist"}
        )
        assert response.status_code == 404

    def test_no_session_returns_404(self, client: TestClient):
        response = client.get("/api/echo/snapshot")
        assert response.status_code == 404

    def test_unsupported_loop_id_filtered_out(self, client: TestClient, repos):
        _seed_session(repos)
        _seed_loop(repos, loop_id="pressure")
        # Manually insert a forward-compat row.
        asyncio.run(
            repos["loop_states"].upsert(
                EchoLoopState(
                    user_id="test-user-123",
                    loop_id="clarity",
                    tone_state="rising",
                    intensity_score=0.7,
                    intensity_label="High",
                )
            )
        )
        response = client.get("/api/echo/snapshot")
        assert response.status_code == 200
        loop_ids = [l["loop_id"] for l in response.json()["data"]["loops"]]
        assert loop_ids == ["pressure"]

    def test_emits_echo_signature_view_event(self, client: TestClient, repos, emitter):
        _seed_session(repos)
        _seed_loop(repos, loop_id="pressure", intensity_score=0.74)
        _seed_loop(repos, loop_id="grief", intensity_score=0.58, tone_state="softening")

        client.get("/api/echo/snapshot")

        view_events = [e for e in emitter.events if e["event"] == "echo_signature_view"]
        assert len(view_events) == 1
        assert view_events[0]["loops_count"] == 2
        assert view_events[0]["motif_id"] == "spiral"


# ============================================================
# POST /dev/echo/loop-state
# ============================================================


class TestDevSeedEndpoint:
    def test_seeds_loop_state_for_user(self, client: TestClient, repos):
        _seed_session(repos)
        payload = {
            "loops": [
                {
                    "loop_id": "pressure",
                    "tone_state": "rising",
                    "intensity_score": 0.7,
                },
                {
                    "loop_id": "grief",
                    "tone_state": "softening",
                    "intensity_score": 0.5,
                },
            ]
        }
        response = client.post("/api/dev/echo/loop-state", json=payload)
        assert response.status_code == 200
        assert response.json()["data"]["seeded"] == 2

        loops = asyncio.run(repos["loop_states"].query_by_user("test-user-123"))
        assert {l.loop_id for l in loops} == {"pressure", "grief"}

    def test_seed_replaces_existing_rows(self, client: TestClient, repos):
        _seed_session(repos)
        _seed_loop(repos, loop_id="pressure")
        _seed_loop(repos, loop_id="agency")

        # Seed only "overwhelm" — pressure + agency should be wiped.
        response = client.post(
            "/api/dev/echo/loop-state",
            json={
                "loops": [
                    {
                        "loop_id": "overwhelm",
                        "tone_state": "rising",
                        "intensity_score": 0.8,
                    }
                ]
            },
        )
        assert response.status_code == 200
        loops = asyncio.run(repos["loop_states"].query_by_user("test-user-123"))
        assert [l.loop_id for l in loops] == ["overwhelm"]

    def test_unsupported_loop_id_returns_400(self, client: TestClient, repos):
        _seed_session(repos)
        response = client.post(
            "/api/dev/echo/loop-state",
            json={
                "loops": [
                    # 'clarity' isn't a V1 loop. Pydantic Literal in the model
                    # rejects it before our handler — yields 422.
                    {
                        "loop_id": "clarity",
                        "tone_state": "rising",
                        "intensity_score": 0.7,
                    }
                ]
            },
        )
        assert response.status_code == 422

    def test_returns_404_in_production(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ENVIRONMENT", "production")
        response = client.post(
            "/api/dev/echo/loop-state",
            json={
                "loops": [
                    {
                        "loop_id": "pressure",
                        "tone_state": "rising",
                        "intensity_score": 0.7,
                    }
                ]
            },
        )
        assert response.status_code == 404


# ============================================================
# POST /echo/recommend-practice
# ============================================================


class TestRecommendPracticeEndpoint:
    def test_happy_path_pressure_rising(self, client: TestClient, repos):
        session = _seed_session(repos)
        _seed_loop(repos, loop_id="pressure", intensity_score=0.74)

        response = client.post(
            "/api/echo/recommend-practice",
            json={"session_id": session.session_id, "surface": "echo_signature"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["pattern"]["loop_id"] == "pressure"
        assert data["rule_id"] == "pressure_loop_v1"
        assert data["practice"]["id"] in {
            "breath_4_6",
            "reappraisal_alt_intent",
            "one_percent_first_sentence",
        }
        assert data["private_mode_active"] is False

    def test_grief_rising_returns_fallback(self, client: TestClient, repos):
        session = _seed_session(repos)
        _seed_loop(repos, loop_id="grief", intensity_score=0.80, tone_state="rising")
        response = client.post(
            "/api/echo/recommend-practice",
            json={
                "session_id": session.session_id,
                "selected_loop": "grief",
                "surface": "mirror_moment",
            },
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["rule_id"] == "fallback"
        assert data["practice"]["id"] == "breath_4_6"

    def test_no_active_loops_returns_404(self, client: TestClient, repos):
        session = _seed_session(repos)
        _seed_loop(
            repos,
            loop_id="pressure",
            intensity_score=0.3,
            tone_state="rising",
            recently_changed=False,
        )
        response = client.post(
            "/api/echo/recommend-practice",
            json={"session_id": session.session_id},
        )
        assert response.status_code == 404

    def test_unsupported_loop_returns_422_via_pydantic(self, client: TestClient, repos):
        session = _seed_session(repos)
        _seed_loop(repos)
        response = client.post(
            "/api/echo/recommend-practice",
            json={
                "session_id": session.session_id,
                "selected_loop": "clarity",
            },
        )
        assert response.status_code == 422

    def test_no_breathwork_filters_breath(self, client: TestClient, repos):
        session = _seed_session(repos)
        _seed_loop(repos, loop_id="pressure", intensity_score=0.74)
        asyncio.run(repos["prefs"].set_flags("test-user-123", no_breathwork=True))

        response = client.post(
            "/api/echo/recommend-practice",
            json={"session_id": session.session_id},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["practice"]["type"] != "breath"

    def test_cooldown_falls_through_to_fallback(self, client: TestClient, repos):
        session = _seed_session(repos)
        _seed_loop(
            repos, loop_id="overwhelm", tone_state="rising", intensity_score=0.65
        )
        recent_iso = (
            (datetime.now(timezone.utc) - timedelta(hours=1))
            .isoformat()
            .replace("+00:00", "Z")
        )
        for pid in ["breath_box_4", "name_and_need", "boundary_prompt"]:
            asyncio.run(
                repos["completions"].put(
                    PracticeCompletion(
                        user_id="test-user-123",
                        session_id=session.session_id,
                        loop_id="overwhelm",
                        tone_state="rising",
                        practice_id=pid,
                        rule_id="overwhelm_v1",
                        completed_at=recent_iso,
                    )
                )
            )

        response = client.post(
            "/api/echo/recommend-practice",
            json={"session_id": session.session_id},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["rule_id"] == "fallback"
        assert data["practice"]["id"] == "breath_4_6"

    def test_selected_loop_overrides_top_of_snapshot(self, client: TestClient, repos):
        session = _seed_session(repos)
        _seed_loop(repos, loop_id="pressure", intensity_score=0.85)
        _seed_loop(
            repos, loop_id="overwhelm", intensity_score=0.65, tone_state="rising"
        )
        response = client.post(
            "/api/echo/recommend-practice",
            json={
                "session_id": session.session_id,
                "selected_loop": "overwhelm",
            },
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["pattern"]["loop_id"] == "overwhelm"
        assert data["rule_id"] == "overwhelm_v1"

    def test_private_mode_flag_in_response(self, client: TestClient, repos):
        session = _seed_session(repos)
        _seed_loop(repos, loop_id="pressure", intensity_score=0.74)
        asyncio.run(repos["prefs"].set_flags("test-user-123", private_mode=True))

        response = client.post(
            "/api/echo/recommend-practice",
            json={"session_id": session.session_id},
        )
        assert response.status_code == 200
        assert response.json()["data"]["private_mode_active"] is True
