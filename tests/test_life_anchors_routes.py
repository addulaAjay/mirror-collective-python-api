"""Integration tests for /me/life-anchors CRUD (MirrorGPT Memory — Phase 2A)."""

from __future__ import annotations

from typing import Any, Dict, Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.app.api.life_anchors_routes import (
    get_life_anchor_repo,
    get_life_anchor_structurer,
)
from src.app.core.security import get_current_user
from src.app.handler import app
from src.app.repositories.life_anchor_repo import LifeAnchorRepo
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

TABLE = "mc_life_anchors-test"


async def _fake_user() -> Dict[str, Any]:
    return {"id": "u-123", "sub": "u-123", "email": "t@example.com"}


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> LifeAnchorRepo:
    monkeypatch.setenv("DYNAMODB_LIFE_ANCHORS_TABLE", TABLE)
    sess = FakeAioSession(
        {
            TABLE: FakeTable(
                primary_key=["user_id", "anchor_id"],
                indexes={"status-index": ["user_id", "status"]},
            )
        }
    )
    return LifeAnchorRepo(session=sess)


@pytest.fixture
def fake_structurer() -> AsyncMock:
    """Structurer stub — tests set .structure.return_value. Default: no LLM data."""
    stub = AsyncMock()
    stub.structure = AsyncMock(return_value=None)
    return stub


@pytest.fixture
def client(repo: LifeAnchorRepo, fake_structurer: AsyncMock) -> Iterator[TestClient]:
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_life_anchor_repo] = lambda: repo
    app.dependency_overrides[get_life_anchor_structurer] = lambda: fake_structurer
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_life_anchor_repo, None)
        app.dependency_overrides.pop(get_life_anchor_structurer, None)


def _create(client: TestClient, **overrides) -> Any:
    body: Dict[str, Any] = {
        "anchor_type": "loss",
        "title": "Wife passed away",
        "emotional_weight": "sacred",
        "reflection_use": "always_consider",
        "relationship": "wife",
        "tone_guidance": ["Do not say time heals everything."],
    }
    body.update(overrides)
    return client.post("/api/me/life-anchors", json=body)


class TestCreateAndList:
    def test_create_then_list(self, client: TestClient):
        r = _create(client)
        assert r.status_code == 201
        data = r.json()["data"]
        assert data["anchor_type"] == "loss"
        assert data["emotional_weight"] == "sacred"
        assert data["scopes"]["mirrorgpt"] is True
        assert data["created_from"] == "manual"
        assert data["anchor_id"]

        lr = client.get("/api/me/life-anchors")
        assert lr.status_code == 200
        items = lr.json()["data"]
        assert len(items) == 1
        assert items[0]["title"] == "Wife passed away"

    def test_create_rejects_bad_enum(self, client: TestClient):
        assert _create(client, emotional_weight="bogus").status_code == 422

    def test_create_requires_nonempty_title(self, client: TestClient):
        assert (
            client.post("/api/me/life-anchors", json={"title": ""}).status_code == 422
        )


class TestUpdatePauseDelete:
    def test_update_is_partial(self, client: TestClient):
        aid = _create(client).json()["data"]["anchor_id"]
        r = client.put(f"/api/me/life-anchors/{aid}", json={"reflection_use": "never"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["reflection_use"] == "never"
        # Untouched fields are preserved.
        assert data["title"] == "Wife passed away"
        assert data["emotional_weight"] == "sacred"

    def test_update_scopes(self, client: TestClient):
        aid = _create(client).json()["data"]["anchor_id"]
        r = client.put(
            f"/api/me/life-anchors/{aid}",
            json={"scopes": {"mirrorgpt": False, "echo_vault": True}},
        )
        assert r.status_code == 200
        assert r.json()["data"]["scopes"]["mirrorgpt"] is False
        assert r.json()["data"]["scopes"]["echo_vault"] is True

    def test_pause(self, client: TestClient):
        aid = _create(client).json()["data"]["anchor_id"]
        r = client.post(f"/api/me/life-anchors/{aid}/pause")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "paused"

    def test_delete(self, client: TestClient):
        aid = _create(client).json()["data"]["anchor_id"]
        assert client.delete(f"/api/me/life-anchors/{aid}").status_code == 200
        assert client.get("/api/me/life-anchors").json()["data"] == []

    def test_update_missing_returns_404(self, client: TestClient):
        assert (
            client.put("/api/me/life-anchors/nope", json={"title": "x"}).status_code
            == 404
        )

    def test_delete_missing_returns_404(self, client: TestClient):
        assert client.delete("/api/me/life-anchors/nope").status_code == 404


class TestConfirm:
    def test_remember_uses_structured_output(
        self, client: TestClient, fake_structurer: AsyncMock
    ):
        fake_structurer.structure.return_value = {
            "anchor_type": "loss",
            "title": "User's wife passed away",
            "relationship": "wife",
            "emotional_weight": "sacred",
            "tone_guidance": ["Do not say time heals everything."],
        }
        r = client.post(
            "/api/me/life-anchors/confirm",
            json={"candidate_text": "my wife died last year", "choice": "remember"},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["anchor_type"] == "loss"
        assert data["title"] == "User's wife passed away"
        assert data["relationship"] == "wife"
        assert data["emotional_weight"] == "sacred"
        # Sacred anchors are always considered.
        assert data["reflection_use"] == "always_consider"
        assert data["created_from"] == "mirrorgpt"
        # It is persisted — shows up in the list.
        assert len(client.get("/api/me/life-anchors").json()["data"]) == 1

    def test_remember_falls_back_when_structurer_returns_none(
        self, client: TestClient, fake_structurer: AsyncMock
    ):
        fake_structurer.structure.return_value = None  # LLM failed
        r = client.post(
            "/api/me/life-anchors/confirm",
            json={
                "candidate_text": "I got a big promotion today",
                "choice": "remember",
                "anchor_type": "transition",
                "emotional_weight": "medium",
                "title": "Got a promotion",
            },
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["anchor_type"] == "transition"
        assert data["title"] == "Got a promotion"
        assert data["reflection_use"] == "when_relevant"

    def test_not_now_writes_nothing(self, client: TestClient):
        r = client.post(
            "/api/me/life-anchors/confirm",
            json={"candidate_text": "something", "choice": "not_now"},
        )
        assert r.status_code == 200
        assert r.json()["data"] is None
        assert client.get("/api/me/life-anchors").json()["data"] == []

    def test_never_writes_nothing(self, client: TestClient):
        r = client.post(
            "/api/me/life-anchors/confirm",
            json={"candidate_text": "something", "choice": "never"},
        )
        assert r.status_code == 200
        assert client.get("/api/me/life-anchors").json()["data"] == []

    def test_rejects_bad_choice(self, client: TestClient):
        r = client.post(
            "/api/me/life-anchors/confirm",
            json={"candidate_text": "x", "choice": "maybe"},
        )
        assert r.status_code == 422
