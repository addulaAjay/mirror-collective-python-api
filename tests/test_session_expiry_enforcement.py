"""Expired-session enforcement (sister of test_session_lifecycle.py).

Spec §6.1: sessions expire at next midnight in user_tz. Once expired, the FE
must show the "take the quiz" affordance instead of stale data. These tests
verify each read/write path returns 404 SESSION_EXPIRED for an expired
session, except PATCH /practice/complete/{id}/helpful which tolerates late
votes.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterator, List
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from src.app.api.echo_v1_routes import (
    get_echo_loop_state_repo,
    get_practice_completion_repo,
    get_reflection_session_repo,
    get_telemetry_emitter,
    get_user_personalization_repo,
)
from src.app.api.practice_routes import get_echo_loop_state_repo as get_loops_practice
from src.app.api.practice_routes import (
    get_practice_completion_repo as get_completions_practice,
)
from src.app.api.practice_routes import (
    get_reflection_session_repo as get_sessions_practice,
)
from src.app.api.practice_routes import get_telemetry_emitter as get_telemetry_practice
from src.app.api.practice_routes import (
    get_user_personalization_repo as get_prefs_practice,
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
USER_ID = "test-user-123"


async def _fake_user() -> Dict[str, Any]:
    return {"id": USER_ID, "sub": USER_ID, "email": "test@example.com"}


class _SpyEmitter:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def emit(self, event_name: str, *, user_hash: str, **fields: Any) -> None:
        self.events.append({"event": event_name, "user_hash": user_hash, **fields})


@pytest.fixture
def fake_tables():
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
def repos(fake_tables, monkeypatch: pytest.MonkeyPatch):
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
def emitter():
    return _SpyEmitter()


@pytest.fixture
def client(repos, emitter) -> Iterator[TestClient]:
    overrides = {
        get_current_user: _fake_user,
        # echo_v1_routes
        get_reflection_session_repo: lambda: repos["sessions"],
        get_echo_loop_state_repo: lambda: repos["loop_states"],
        get_practice_completion_repo: lambda: repos["completions"],
        get_user_personalization_repo: lambda: repos["prefs"],
        get_telemetry_emitter: lambda: emitter,
        # practice_routes
        get_sessions_practice: lambda: repos["sessions"],
        get_loops_practice: lambda: repos["loop_states"],
        get_completions_practice: lambda: repos["completions"],
        get_prefs_practice: lambda: repos["prefs"],
        get_telemetry_practice: lambda: emitter,
    }
    for dep, factory in overrides.items():
        app.dependency_overrides[dep] = factory
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overrides:
            app.dependency_overrides.pop(dep, None)


def _put_expired_session(repos, **overrides) -> ReflectionSession:
    """Put a session whose expires_at is firmly in the past."""
    base = dict(
        user_id=USER_ID,
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
        expires_at="2020-01-01T00:00:00Z",  # firmly in the past
        created_at="2020-01-01T00:00:00Z",
    )
    base.update(overrides)
    s = ReflectionSession(**base)
    asyncio.run(repos["sessions"].put(s))
    return s


def _put_loop(repos, **overrides) -> EchoLoopState:
    base = dict(
        user_id=USER_ID,
        loop_id="pressure",
        tone_state="rising",
        intensity_score=0.74,
        intensity_label="High",
        last_seen="2020-01-01T20:10:00Z",
    )
    base.update(overrides)
    state = EchoLoopState(**base)
    asyncio.run(repos["loop_states"].upsert(state))
    return state


# ============================================================
# GET /echo/snapshot
# ============================================================


class TestSnapshotRejectsExpired:
    def test_implicit_session_expired_returns_404_session_expired(self, client, repos):
        _put_expired_session(repos)
        _put_loop(repos)  # stale loops still in DDB

        response = client.get("/api/echo/snapshot")
        assert response.status_code == 404
        body = response.json()
        assert body["errorCode"] == "SESSION_EXPIRED"

    def test_explicit_expired_session_id_returns_404(self, client, repos):
        session = _put_expired_session(repos)
        response = client.get(
            "/api/echo/snapshot", params={"session_id": session.session_id}
        )
        assert response.status_code == 404
        assert response.json()["errorCode"] == "SESSION_EXPIRED"

    def test_no_session_at_all_still_returns_plain_404(self, client):
        """Distinguish from SESSION_EXPIRED — no session yet should still
        be a generic NOT_FOUND so the FE can show a different copy if it
        wants to (onboarding vs. daily check-in)."""
        response = client.get("/api/echo/snapshot")
        assert response.status_code == 404
        body = response.json()
        # Either NOT_FOUND or no errorCode — definitely NOT SESSION_EXPIRED.
        assert body.get("errorCode") != "SESSION_EXPIRED"


# ============================================================
# POST /echo/recommend-practice
# ============================================================


class TestRecommendRejectsExpired:
    def test_expired_session_returns_404_session_expired(self, client, repos):
        session = _put_expired_session(repos)
        _put_loop(repos)
        response = client.post(
            "/api/echo/recommend-practice",
            json={"session_id": session.session_id},
        )
        assert response.status_code == 404
        assert response.json()["errorCode"] == "SESSION_EXPIRED"


# ============================================================
# POST /practice/complete
# ============================================================


class TestCompleteRejectsExpired:
    def test_expired_session_returns_404_session_expired(self, client, repos):
        session = _put_expired_session(repos)
        _put_loop(repos)
        response = client.post(
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
        assert response.status_code == 404
        assert response.json()["errorCode"] == "SESSION_EXPIRED"

    def test_expired_session_does_not_log_completion(self, client, repos, emitter):
        """Reject before any DDB writes / personalization changes / telemetry."""
        session = _put_expired_session(repos)
        _put_loop(repos)
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
        # No telemetry emitted.
        assert emitter.events == []
        # No personalization changes.
        prefs = asyncio.run(repos["prefs"].get_or_default(USER_ID))
        assert prefs.recent_use == {}
        assert prefs.practice_helpfulness == {}


# ============================================================
# PATCH /practice/complete/{id}/helpful — tolerant of expired session
# ============================================================


class TestPatchHelpfulToleratesExpired:
    def test_late_vote_works_against_expired_session(self, client, repos):
        """User submitted a practice yesterday, votes "helpful" today after
        the session has aged out. The vote, state delta, and telemetry still
        fire — we just don't return a fresh snapshot."""
        from src.app.models.practice_completion import PracticeCompletion

        session = _put_expired_session(repos)
        _put_loop(repos, intensity_score=0.74)

        # Plant a completion belonging to the now-expired session.
        completion = PracticeCompletion(
            user_id=USER_ID,
            session_id=session.session_id,
            loop_id="pressure",
            tone_state="rising",
            practice_id="breath_4_6",
            rule_id="pressure_loop_v1",
            helpful=None,
            completed_at="2020-01-01T18:00:00Z",
        )
        asyncio.run(repos["completions"].put(completion))

        # Late vote — should succeed.
        response = client.patch(
            f"/api/practice/complete/{quote(completion.completion_id, safe='')}/helpful",
            json={"helpful": True},
        )
        assert response.status_code == 200
        body = response.json()
        # Completion id echoed; snapshot field absent (or None — exclude_none).
        assert body["data"]["completion_id"] == completion.completion_id
        assert "snapshot" not in body["data"]

        # State delta still applied.
        loop = asyncio.run(repos["loop_states"].get(USER_ID, "pressure"))
        assert loop.intensity_score == pytest.approx(0.64, abs=1e-3)
        assert loop.tone_state == "softening"

        # Helpfulness vote recorded.
        prefs = asyncio.run(repos["prefs"].get_or_default(USER_ID))
        assert len(prefs.practice_helpfulness["breath_4_6"]) == 1
        assert prefs.practice_helpfulness["breath_4_6"][0].helpful is True
