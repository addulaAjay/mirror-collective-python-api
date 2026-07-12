"""Integration tests for the Soul Ping mark-read endpoint."""

from __future__ import annotations

from typing import Any, Dict, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.app.core.security import get_current_user
from src.app.handler import app


async def _fake_user() -> Dict[str, Any]:
    return {"id": "u-1", "sub": "u-1", "email": "u1@example.com"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_current_user] = _fake_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_mark_read_success(client: TestClient):
    svc = MagicMock()
    svc.mark_read = AsyncMock(return_value=True)
    with patch("src.app.api.soul_ping_routes.get_soul_ping_service", return_value=svc):
        r = client.post("/api/soul-pings/ping-123/read")
    assert r.status_code == 200
    assert r.json() == {"success": True, "ping_id": "ping-123"}
    svc.mark_read.assert_awaited_once_with("u-1", "ping-123")


def test_mark_read_404_when_not_found(client: TestClient):
    svc = MagicMock()
    svc.mark_read = AsyncMock(return_value=False)
    with patch("src.app.api.soul_ping_routes.get_soul_ping_service", return_value=svc):
        r = client.post("/api/soul-pings/nope/read")
    assert r.status_code == 404
