"""Integration tests for telemetry beacon endpoints (spec §10)."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

from src.app.api.telemetry_routes import get_telemetry_emitter
from src.app.core.security import get_current_user
from src.app.handler import app


async def _fake_user() -> Dict[str, Any]:
    return {"id": "test-user-123", "sub": "test-user-123", "email": "test@example.com"}


class _SpyEmitter:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def emit(self, event_name: str, *, user_hash: str, **fields: Any) -> None:
        self.events.append({"event": event_name, "user_hash": user_hash, **fields})


@pytest.fixture
def emitter() -> _SpyEmitter:
    return _SpyEmitter()


@pytest.fixture
def client(emitter) -> Iterator[TestClient]:
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_telemetry_emitter] = lambda: emitter
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_telemetry_emitter, None)


class TestPracticeExpandBeacon:
    def test_emits_event(self, client, emitter):
        response = client.post(
            "/api/telemetry/practice-expand",
            json={"loop_id": "pressure", "practice_id": "breath_4_6"},
        )
        assert response.status_code == 204
        assert len(emitter.events) == 1
        ev = emitter.events[0]
        assert ev["event"] == "practice_expand"
        assert ev["loop_id"] == "pressure"
        assert ev["practice_id"] == "breath_4_6"
        assert ev["user_hash"]  # populated

    def test_invalid_loop_id_returns_422(self, client):
        response = client.post(
            "/api/telemetry/practice-expand",
            json={"loop_id": "clarity", "practice_id": "breath_4_6"},
        )
        assert response.status_code == 422


class TestNudgeOpenedBeacon:
    def test_emits_event(self, client, emitter):
        response = client.post(
            "/api/telemetry/nudge-opened",
            json={"nudge_type": "morning_check_in"},
        )
        assert response.status_code == 204
        ev = emitter.events[0]
        assert ev["event"] == "nudge_opened"
        assert ev["nudge_type"] == "morning_check_in"


class TestEchoMapRefreshBeacon:
    def test_emits_event_no_body(self, client, emitter):
        response = client.post("/api/telemetry/echo-map-refresh")
        assert response.status_code == 204
        ev = emitter.events[0]
        assert ev["event"] == "echo_map_refresh"
        assert "user_hash" in ev
