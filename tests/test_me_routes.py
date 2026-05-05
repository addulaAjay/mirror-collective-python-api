"""Integration tests for /me preferences + private-mode-reveal (spec §10.1)."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

from src.app.api.me_routes import get_telemetry_emitter, get_user_personalization_repo
from src.app.core.security import get_current_user
from src.app.handler import app
from src.app.repositories.user_personalization_repo import UserPersonalizationRepo
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

PREFS = "mc_user_personalization-test"


async def _fake_user() -> Dict[str, Any]:
    return {"id": "test-user-123", "sub": "test-user-123", "email": "test@example.com"}


class _SpyEmitter:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def emit(self, event_name: str, *, user_hash: str, **fields: Any) -> None:
        self.events.append({"event": event_name, "user_hash": user_hash, **fields})


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> UserPersonalizationRepo:
    monkeypatch.setenv("DYNAMODB_USER_PERSONALIZATION_TABLE", PREFS)
    sess = FakeAioSession({PREFS: FakeTable(primary_key=["user_id"])})
    return UserPersonalizationRepo(session=sess)


@pytest.fixture
def emitter() -> _SpyEmitter:
    return _SpyEmitter()


@pytest.fixture
def client(repo, emitter) -> Iterator[TestClient]:
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_user_personalization_repo] = lambda: repo
    app.dependency_overrides[get_telemetry_emitter] = lambda: emitter
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_user_personalization_repo, None)
        app.dependency_overrides.pop(get_telemetry_emitter, None)


# ============================================================
# GET /me/preferences
# ============================================================


class TestGetPreferences:
    def test_default_for_new_user(self, client):
        response = client.get("/api/me/preferences")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["flags"]["no_breathwork"] is False
        assert data["flags"]["reduced_motion"] is False
        assert data["flags"]["private_mode"] is False
        assert data["disallow_types"] == []


# ============================================================
# PUT /me/preferences/flags
# ============================================================


class TestUpdateFlags:
    def test_set_no_breathwork_true(self, client):
        response = client.put("/api/me/preferences/flags", json={"no_breathwork": True})
        assert response.status_code == 200
        flags = response.json()["data"]
        assert flags["no_breathwork"] is True
        assert flags["reduced_motion"] is False
        assert flags["private_mode"] is False

    def test_partial_update_preserves_other_flags(self, client):
        # First set private_mode=True.
        client.put("/api/me/preferences/flags", json={"private_mode": True})
        # Then set reduced_motion=True without touching private_mode.
        response = client.put(
            "/api/me/preferences/flags", json={"reduced_motion": True}
        )
        flags = response.json()["data"]
        assert flags["private_mode"] is True
        assert flags["reduced_motion"] is True

    def test_empty_body_no_change(self, client):
        client.put("/api/me/preferences/flags", json={"no_breathwork": True})
        response = client.put("/api/me/preferences/flags", json={})
        assert response.status_code == 200
        flags = response.json()["data"]
        assert flags["no_breathwork"] is True

    def test_get_after_put_round_trips(self, client):
        client.put(
            "/api/me/preferences/flags",
            json={"no_breathwork": True, "private_mode": True},
        )
        response = client.get("/api/me/preferences")
        flags = response.json()["data"]["flags"]
        assert flags["no_breathwork"] is True
        assert flags["private_mode"] is True
        assert flags["reduced_motion"] is False


# ============================================================
# POST /me/private-mode/reveal
# ============================================================


class TestPrivateModeReveal:
    def test_emits_event_with_surface(self, client, emitter):
        response = client.post(
            "/api/me/private-mode/reveal",
            json={"surface": "echo_signature"},
        )
        assert response.status_code == 204
        assert len(emitter.events) == 1
        ev = emitter.events[0]
        assert ev["event"] == "private_mode_reveal"
        assert ev["surface"] == "echo_signature"
        assert ev["user_hash"]

    def test_invalid_surface_returns_422(self, client):
        response = client.post(
            "/api/me/private-mode/reveal",
            json={"surface": "instagram"},
        )
        assert response.status_code == 422
