"""Integration tests for Reflection Room V1 endpoints (spec §B.3.1, §B.3.5).

Drives the FastAPI TestClient with the in-memory FakeDynamoDB shim wired in
via ``app.dependency_overrides``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator

import pytest
from fastapi.testclient import TestClient

from src.app.api.reflection_routes import (
    get_echo_loop_state_repo,
    get_reflection_session_repo,
)
from src.app.core.security import get_current_user
from src.app.handler import app
from src.app.repositories.echo_loop_state_repo import EchoLoopStateRepo
from src.app.repositories.reflection_session_repo import (
    GSI_USER_CREATED,
    ReflectionSessionRepo,
)
from src.app.services.reflection import session_lifecycle
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable


# conftest.py installs a global ``mock_get_current_user(*args, **kwargs)`` whose
# variadic signature trips FastAPI's signature inspection (treats ``args`` /
# ``kwargs`` as missing query params → 422). We replace it locally with a
# correctly-typed no-arg coroutine so the request reaches the handler.
async def _fake_user_no_varargs() -> Dict[str, Any]:
    return {
        "id": "test-user-123",
        "sub": "test-user-123",
        "email": "test@example.com",
        "given_name": "Test",
        "family_name": "User",
    }


SESSIONS_TABLE = "mc_reflection_sessions-test"
LOOP_STATE_TABLE = "mc_echo_loop_state-test"


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def fake_tables() -> Dict[str, FakeTable]:
    return {
        SESSIONS_TABLE: FakeTable(
            primary_key=["session_id"],
            indexes={GSI_USER_CREATED: ["user_id", "created_at"]},
        ),
        LOOP_STATE_TABLE: FakeTable(primary_key=["user_id", "loop_id"]),
    }


@pytest.fixture
def repos(
    fake_tables: Dict[str, FakeTable], monkeypatch: pytest.MonkeyPatch
) -> Dict[str, Any]:
    monkeypatch.setenv("DYNAMODB_REFLECTION_SESSIONS_TABLE", SESSIONS_TABLE)
    monkeypatch.setenv("DYNAMODB_ECHO_LOOP_STATE_TABLE", LOOP_STATE_TABLE)
    fake_session = FakeAioSession(fake_tables)
    return {
        "sessions": ReflectionSessionRepo(session=fake_session),
        "loop_states": EchoLoopStateRepo(session=fake_session),
    }


@pytest.fixture
def client(repos: Dict[str, Any]) -> Iterator[TestClient]:
    """TestClient with the fake repos + clean auth wired into FastAPI."""
    app.dependency_overrides[get_current_user] = _fake_user_no_varargs
    app.dependency_overrides[get_reflection_session_repo] = lambda: repos["sessions"]
    app.dependency_overrides[get_echo_loop_state_repo] = lambda: repos["loop_states"]
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_reflection_session_repo, None)
        app.dependency_overrides.pop(get_echo_loop_state_repo, None)


CANONICAL_SPIRAL = {
    "answers": {
        "q1": "hopeful",
        "q2": "inspiration",
        "q3": "spiral",
        "q4": "insight",
    }
}


# ============================================================
# POST /reflection/quiz — happy path
# ============================================================


class TestQuizHappyPath:
    def test_returns_200_with_motif_payload(self, client: TestClient):
        response = client.post("/api/reflection/quiz", json=CANONICAL_SPIRAL)
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        data = body["data"]
        assert "session_id" in data
        motif = data["motif"]
        assert motif["motif_id"] == "spiral"
        assert motif["motif_name"] == "Spiral"
        assert motif["room_skin"] == "Spiral Room"
        assert motif["override_allowed"] is False

    def test_seeds_loop_state_for_user(self, client: TestClient, repos):
        import asyncio

        response = client.post("/api/reflection/quiz", json=CANONICAL_SPIRAL)
        assert response.status_code == 200

        loops = asyncio.run(repos["loop_states"].query_by_user("test-user-123"))
        loop_ids = sorted(s.loop_id for s in loops)
        # Canonical Spiral seeds agency + transition.
        assert loop_ids == ["agency", "transition"]

    def test_writes_session_row_with_expires_at(self, client: TestClient, repos):
        import asyncio

        response = client.post("/api/reflection/quiz", json=CANONICAL_SPIRAL)
        assert response.status_code == 200

        latest = asyncio.run(repos["sessions"].get_latest_for_user("test-user-123"))
        assert latest is not None
        assert latest.motif_id == "spiral"
        assert latest.expires_at  # populated from default tz
        assert latest.user_tz == "America/New_York"
        assert latest.quiz_answers == CANONICAL_SPIRAL["answers"]


# ============================================================
# POST /reflection/quiz — invalid input
# ============================================================


class TestQuizValidation:
    def test_invalid_q1_answer_returns_422(self, client: TestClient):
        # Pydantic Literal mismatch → FastAPI 422.
        response = client.post(
            "/api/reflection/quiz",
            json={
                "answers": {
                    "q1": "purple",
                    "q2": "inspiration",
                    "q3": "spiral",
                    "q4": "insight",
                }
            },
        )
        assert response.status_code == 422

    def test_missing_question_returns_422(self, client: TestClient):
        response = client.post(
            "/api/reflection/quiz",
            json={"answers": {"q1": "hopeful", "q2": "inspiration", "q3": "spiral"}},
        )
        assert response.status_code == 422


# ============================================================
# POST /reflection/quiz — session reuse / overwrite (spec §6.1)
# ============================================================


class TestSessionReuse:
    def test_same_answers_within_active_session_reuses_session(
        self, client: TestClient
    ):
        first = client.post("/api/reflection/quiz", json=CANONICAL_SPIRAL).json()
        second = client.post("/api/reflection/quiz", json=CANONICAL_SPIRAL).json()
        assert first["data"]["session_id"] == second["data"]["session_id"]

    def test_different_answers_within_active_session_reuses_session_id_with_new_motif(
        self, client: TestClient, repos
    ):
        import asyncio

        first = client.post("/api/reflection/quiz", json=CANONICAL_SPIRAL).json()
        first_id = first["data"]["session_id"]

        # Different answers → mirror motif (q3=mirror → reflection tag).
        second_payload = {
            "answers": {"q1": "numb", "q2": "peace", "q3": "mirror", "q4": "presence"}
        }
        second = client.post("/api/reflection/quiz", json=second_payload).json()
        assert second["data"]["session_id"] == first_id  # reused
        assert second["data"]["motif"]["motif_id"] == "mirror"

        # Confirm the session row was updated (motif rewritten).
        latest = asyncio.run(repos["sessions"].get(first_id))
        assert latest is not None
        assert latest.motif_id == "mirror"

    def test_session_after_midnight_creates_new_session(
        self, client: TestClient, repos, monkeypatch: pytest.MonkeyPatch
    ):
        import asyncio

        # Submit once.
        first = client.post("/api/reflection/quiz", json=CANONICAL_SPIRAL).json()
        first_id = first["data"]["session_id"]

        # Force the existing session to look expired by rewriting expires_at into the past.
        latest = asyncio.run(repos["sessions"].get(first_id))
        assert latest is not None
        latest.expires_at = "2026-01-01T00:00:00Z"
        asyncio.run(repos["sessions"].put(latest))

        # Re-submit — should create a brand-new session row.
        second = client.post("/api/reflection/quiz", json=CANONICAL_SPIRAL).json()
        assert second["data"]["session_id"] != first_id


# ============================================================
# POST /reflection/quiz — timezone resolution (spec §6.1)
# ============================================================


class TestTimezoneResolution:
    def test_default_tz_when_no_header(self, client: TestClient, repos):
        import asyncio

        response = client.post("/api/reflection/quiz", json=CANONICAL_SPIRAL)
        assert response.status_code == 200
        latest = asyncio.run(repos["sessions"].get_latest_for_user("test-user-123"))
        assert latest.user_tz == "America/New_York"

    def test_header_overrides_default_tz(self, client: TestClient, repos):
        import asyncio

        response = client.post(
            "/api/reflection/quiz",
            json=CANONICAL_SPIRAL,
            headers={"X-User-Timezone": "Asia/Tokyo"},
        )
        assert response.status_code == 200
        latest = asyncio.run(repos["sessions"].get_latest_for_user("test-user-123"))
        assert latest.user_tz == "Asia/Tokyo"

    def test_invalid_header_falls_back_to_default(self, client: TestClient, repos):
        import asyncio

        response = client.post(
            "/api/reflection/quiz",
            json=CANONICAL_SPIRAL,
            headers={"X-User-Timezone": "Mars/OlympusMons"},
        )
        assert response.status_code == 200
        latest = asyncio.run(repos["sessions"].get_latest_for_user("test-user-123"))
        assert latest.user_tz == "America/New_York"


# ============================================================
# PUT /me/reflection/room
# ============================================================


class TestRoomSkinOverride:
    def test_apply_session_override(self, client: TestClient, repos):
        """Submit a quiz that produces a tie, then override to a different motif."""
        import asyncio

        # First, write a session whose stored motif_payload says override_allowed=true.
        # Easiest path: bypass the quiz endpoint and put a hand-rolled session row.
        from src.app.models.reflection_session import ReflectionSession

        now = session_lifecycle.now_utc()
        created, expires, ttl = session_lifecycle.compute_session_window(
            "America/New_York", now
        )
        session = ReflectionSession(
            user_id="test-user-123",
            motif_id="spiral",
            motif_name="Spiral",
            room_skin="Spiral Room",
            motif_payload={
                "motif_id": "spiral",
                "motif_name": "Spiral",
                "icon": "🌀",
                "element": "Fire",
                "tone_tag": "Evolution / Integration",
                "why_text": "...",
                "room_skin": "Spiral Room",
                "scores": {"evolution": 5},
                "explanation": [],
                "override_allowed": True,
            },
            quiz_answers={
                "q1": "hopeful",
                "q2": "inspiration",
                "q3": "spiral",
                "q4": "insight",
            },
            scores={"evolution": 5},
            user_tz="America/New_York",
            expires_at=expires,
            created_at=created,
            updated_at=created,
            ttl=ttl,
        )
        asyncio.run(repos["sessions"].put(session))

        response = client.put(
            "/api/me/reflection/room",
            json={"motif_id": "mirror", "apply_to": "session"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["motif"]["motif_id"] == "mirror"
        assert data["motif"]["room_skin"] == "Mirror Room"
        assert data["applied_to"] == "session"

        # Stored session row reflects override.
        latest = asyncio.run(repos["sessions"].get(session.session_id))
        assert latest.room_skin_override == "Mirror Room"
        assert latest.effective_room_skin() == "Mirror Room"

    def test_override_blocked_when_override_not_allowed(
        self, client: TestClient, repos
    ):
        """Quiz produced a unique winner → override returns 403."""
        import asyncio

        # Submit normal quiz (override_allowed=False).
        client.post("/api/reflection/quiz", json=CANONICAL_SPIRAL)

        response = client.put(
            "/api/me/reflection/room",
            json={"motif_id": "mirror", "apply_to": "session"},
        )
        assert response.status_code == 403
        assert (
            "OVERRIDE" in response.text.upper() or "override" in response.text.lower()
        )

    def test_unknown_motif_id_returns_400(self, client: TestClient, repos):
        """Override target must exist in motif_mapping."""
        import asyncio

        from src.app.models.reflection_session import ReflectionSession

        now = session_lifecycle.now_utc()
        created, expires, ttl = session_lifecycle.compute_session_window(
            "America/New_York", now
        )
        # Manually persist a session with override_allowed=True so the override
        # is reachable; we expect the 400 to come from the unknown motif id.
        session = ReflectionSession(
            user_id="test-user-123",
            motif_payload={"override_allowed": True},
            quiz_answers={
                "q1": "hopeful",
                "q2": "inspiration",
                "q3": "spiral",
                "q4": "insight",
            },
            scores={"evolution": 5},
            user_tz="America/New_York",
            expires_at=expires,
            created_at=created,
            updated_at=created,
            ttl=ttl,
        )
        asyncio.run(repos["sessions"].put(session))

        response = client.put(
            "/api/me/reflection/room",
            json={"motif_id": "banana", "apply_to": "session"},
        )
        assert response.status_code == 400

    def test_override_with_no_session_returns_404(self, client: TestClient):
        """Brand-new user with no quiz session → 404."""
        response = client.put(
            "/api/me/reflection/room",
            json={"motif_id": "mirror", "apply_to": "session"},
        )
        assert response.status_code == 404
