"""Guard tests for POST /api/register-device.

Older iOS app builds (pre-2026-06-10) sent the Firebase FCM token instead of the
raw APNs token. Forwarded to the SNS APNS platform application, that produced
``InvalidParameter ... iOS device tokens must be no more than 400 hexadecimal
characters`` — which the route turned into a blanket HTTP 500.

These tests pin the backend guard:
  * unsupported platform -> 400
  * malformed iOS token (non-hex / too long / empty) -> 400
  * a valid iOS token still registers
  * an Android FCM token (non-hex) is NOT hex-validated
  * a runtime SNS failure is non-fatal (never a 500)
"""

from typing import Any, Dict, Iterator
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

import src.app.api.routes as routes_module
from src.app.core.security import get_current_user
from src.app.handler import app

VALID_IOS_TOKEN = "a1b2c3d4" * 8  # 64 hex chars, APNs-shaped
FCM_LIKE_TOKEN = "cdE:APA91bFxyz_" + "z" * 180  # long, non-hex (':' '_' 'x')


async def _fake_user() -> Dict[str, Any]:
    return {"id": "test-user-123", "sub": "test-user-123", "email": "test@example.com"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Authed TestClient with a clean get_current_user override.

    The conftest override uses a ``*args, **kwargs`` signature which FastAPI
    misreads as required query params on authenticated routes, so we set our own.
    """
    app.dependency_overrides[get_current_user] = _fake_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _mock_persistence(monkeypatch, *, existing=None, endpoint="arn:aws:sns:end"):
    monkeypatch.setattr(
        routes_module.dynamodb_service,
        "get_device_token",
        AsyncMock(return_value=existing),
    )
    monkeypatch.setattr(
        routes_module.dynamodb_service,
        "cleanup_guest_registration",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        routes_module.dynamodb_service,
        "save_device_token",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        routes_module.sns_service,
        "create_platform_endpoint",
        Mock(return_value=endpoint),
    )
    monkeypatch.setattr(
        routes_module.sns_service,
        "subscribe_to_topic",
        Mock(return_value="arn:aws:sns:sub"),
    )


def test_unsupported_platform_returns_400(client):
    resp = client.post(
        "/api/register-device",
        json={"device_token": VALID_IOS_TOKEN, "platform": "windows"},
    )
    assert resp.status_code == 400
    assert "platform" in resp.text.lower()


def test_ios_fcm_token_returns_400(client):
    """The actual production failure: FCM token sent as iOS -> clean 400, not 500."""
    resp = client.post(
        "/api/register-device",
        json={"device_token": FCM_LIKE_TOKEN, "platform": "ios"},
    )
    assert resp.status_code == 400
    assert "token" in resp.text.lower()


def test_ios_token_too_long_returns_400(client):
    resp = client.post(
        "/api/register-device",
        json={"device_token": "a" * 401, "platform": "ios"},
    )
    assert resp.status_code == 400


def test_empty_token_returns_400(client):
    resp = client.post(
        "/api/register-device",
        json={"device_token": "   ", "platform": "ios"},
    )
    assert resp.status_code == 400


def test_valid_ios_token_registers(client, monkeypatch):
    _mock_persistence(monkeypatch)
    resp = client.post(
        "/api/register-device",
        json={"device_token": VALID_IOS_TOKEN, "platform": "ios"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "registered"


def test_android_fcm_token_allowed(client, monkeypatch):
    """Android FCM tokens are non-hex and must not be rejected by the iOS check."""
    _mock_persistence(monkeypatch)
    resp = client.post(
        "/api/register-device",
        json={"device_token": FCM_LIKE_TOKEN, "platform": "android"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "registered"


def test_sns_failure_is_non_fatal(client, monkeypatch):
    _mock_persistence(monkeypatch)
    monkeypatch.setattr(
        routes_module.sns_service,
        "create_platform_endpoint",
        Mock(side_effect=Exception("InvalidParameter: CreatePlatformEndpoint")),
    )
    resp = client.post(
        "/api/register-device",
        json={"device_token": VALID_IOS_TOKEN, "platform": "ios"},
    )
    assert resp.status_code != 500
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "push_registration_failed"
