"""Route tests for PATCH /api/recipients/{id} (edit recipient picture)."""

from __future__ import annotations

from typing import Any, Dict, Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.app.core.exceptions import ValidationError
from src.app.core.security import get_current_user
from src.app.handler import app
from src.app.models.echo import Recipient

CANON = "https://echo-vault-media.s3.us-east-1.amazonaws.com/profiles/u-1/new.jpg"


async def _fake_user() -> Dict[str, Any]:
    return {"id": "u-1", "sub": "u-1"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_current_user] = _fake_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_patch_recipient_success(client: TestClient):
    updated = Recipient(
        recipient_id="r-1",
        user_id="u-1",
        name="James",
        email="james@email.com",
        profile_image_url="https://presigned.example/get",
    )
    with patch(
        "src.app.api.echo_routes.echo_service.update_recipient_picture",
        new=AsyncMock(return_value=updated),
    ) as m:
        r = client.patch("/api/recipients/r-1", json={"profile_image_url": CANON})

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["recipient_id"] == "r-1"
    assert data["name"] == "James"
    assert data["profile_image_url"] == "https://presigned.example/get"
    m.assert_awaited_once_with("r-1", "u-1", CANON)


def test_patch_recipient_404_when_not_found(client: TestClient):
    with patch(
        "src.app.api.echo_routes.echo_service.update_recipient_picture",
        new=AsyncMock(return_value=None),
    ):
        r = client.patch("/api/recipients/nope", json={"profile_image_url": CANON})
    assert r.status_code == 404


def test_patch_recipient_400_on_validation_error(client: TestClient):
    with patch(
        "src.app.api.echo_routes.echo_service.update_recipient_picture",
        new=AsyncMock(side_effect=ValidationError("outside namespace")),
    ):
        r = client.patch(
            "/api/recipients/r-1",
            json={"profile_image_url": "https://evil.example/x.jpg"},
        )
    assert r.status_code == 400


def test_patch_recipient_422_when_body_missing_picture(client: TestClient):
    r = client.patch("/api/recipients/r-1", json={})
    assert r.status_code == 422  # profile_image_url is required
