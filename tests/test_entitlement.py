"""
Tests for the entitlement gate (src/app/core/entitlement.py).

Covers:
  - Each entitled status passes through and returns an EntitledUser.
  - Each non-entitled status raises 402 with the correct reason code.
  - Missing UserProfile (brand-new signup pre-trial) → 402 reason='free'.
  - Missing user id in claims → 401.
  - Integration smoke: a gated route returns 402 when entitlement fails.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.app.core.entitlement import (
    ENTITLED_STATUSES,
    EntitledUser,
    _lock_reason,
    require_entitled,
)
from src.app.models.user_profile import UserProfile, UserStatus

# --------------------------------------------------------------------------- #
# Unit tests — _lock_reason mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "status_value,expected",
    [
        ("trial_expired", "trial_expired"),
        ("expired", "expired"),
        ("cancelled", "expired"),
        ("none", "free"),
        ("", "free"),
        ("nonsense", "free"),
    ],
)
def test_lock_reason_mapping(status_value: str, expected: str) -> None:
    assert _lock_reason(status_value) == expected


def test_entitled_statuses_set() -> None:
    """Locked entitlement matrix — 2026-05-11. Do not change without
    coordinating with frontend useEntitlement.ts and the docs."""
    assert ENTITLED_STATUSES == frozenset({"trial", "active", "grace_period"})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_profile(status: str, tier: str = "core") -> UserProfile:
    return UserProfile(
        user_id="test-user-123",
        email="test@example.com",
        subscription_tier=tier,
        subscription_status=status,
        echo_vault_quota_gb=(
            50.0 if status in {"trial", "active", "grace_period"} else 0.0
        ),
        echo_vault_used_gb=0.0,
        status=UserStatus.CONFIRMED,
    )


def _patch_dynamodb_get_profile(monkeypatch, profile) -> AsyncMock:
    """Patch the module-level DynamoDBService.get_user_profile in entitlement.py."""
    from src.app.core import entitlement as ent_mod

    fake_service = AsyncMock()
    fake_service.get_user_profile = AsyncMock(return_value=profile)
    monkeypatch.setattr(ent_mod, "_dynamodb_service", fake_service)
    monkeypatch.setattr(ent_mod, "_get_dynamodb_service", lambda: fake_service)
    return fake_service


_FAKE_USER: Dict[str, Any] = {
    "sub": "test-user-123",
    "id": "test-user-123",
    "email": "test@example.com",
}


# --------------------------------------------------------------------------- #
# Unit tests — require_entitled directly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", ["trial", "active", "grace_period"])
@pytest.mark.asyncio
async def test_require_entitled_passes_for_entitled_statuses(monkeypatch, status):
    profile = _make_profile(status)
    _patch_dynamodb_get_profile(monkeypatch, profile)

    result = await require_entitled(current_user=_FAKE_USER)

    assert isinstance(result, EntitledUser)
    assert result.user_id == "test-user-123"
    assert result.profile.subscription_status == status
    assert result.user is _FAKE_USER


@pytest.mark.parametrize(
    "status,expected_reason",
    [
        ("trial_expired", "trial_expired"),
        ("expired", "expired"),
        ("cancelled", "expired"),
        ("none", "free"),
        ("", "free"),
    ],
)
@pytest.mark.asyncio
async def test_require_entitled_raises_402_for_locked_statuses(
    monkeypatch, status, expected_reason
):
    profile = _make_profile(status)
    _patch_dynamodb_get_profile(monkeypatch, profile)

    with pytest.raises(HTTPException) as exc:
        await require_entitled(current_user=_FAKE_USER)

    assert exc.value.status_code == 402
    assert exc.value.detail["code"] == "subscription_required"
    assert exc.value.detail["reason"] == expected_reason


@pytest.mark.asyncio
async def test_require_entitled_raises_402_when_profile_missing(monkeypatch):
    _patch_dynamodb_get_profile(monkeypatch, None)

    with pytest.raises(HTTPException) as exc:
        await require_entitled(current_user=_FAKE_USER)

    assert exc.value.status_code == 402
    assert exc.value.detail["reason"] == "free"


@pytest.mark.asyncio
async def test_require_entitled_raises_401_when_missing_user_id(monkeypatch):
    _patch_dynamodb_get_profile(monkeypatch, _make_profile("active"))

    with pytest.raises(HTTPException) as exc:
        await require_entitled(current_user={"email": "no-id@example.com"})

    assert exc.value.status_code == 401


# --------------------------------------------------------------------------- #
# Integration smoke — a gated route returns 402 when locked
# --------------------------------------------------------------------------- #


def _build_gated_test_app(monkeypatch, profile) -> FastAPI:
    """Build a minimal FastAPI app with one route gated by require_entitled.

    The DynamoDB lookup is patched via monkeypatch so the module-level
    singleton in `src.app.core.entitlement` is restored after the test,
    avoiding cross-test pollution.
    """
    from src.app.core import entitlement as ent_mod
    from src.app.core.error_handlers import setup_error_handlers

    fake_service = AsyncMock()
    fake_service.get_user_profile = AsyncMock(return_value=profile)
    monkeypatch.setattr(ent_mod, "_dynamodb_service", fake_service)
    monkeypatch.setattr(ent_mod, "_get_dynamodb_service", lambda: fake_service)

    app = FastAPI()
    setup_error_handlers(app)

    async def _fake_user_with_profile():
        return _FAKE_USER

    from src.app.core.enhanced_auth import get_user_with_profile

    app.dependency_overrides[get_user_with_profile] = _fake_user_with_profile

    @app.get("/test/gated")
    async def gated(entitled: EntitledUser = Depends(require_entitled)):
        return {"ok": True, "user_id": entitled.user_id}

    return app


def test_gated_route_returns_402_for_expired_user(monkeypatch):
    app = _build_gated_test_app(monkeypatch, _make_profile("trial_expired"))
    with TestClient(app) as client:
        r = client.get("/test/gated")
        assert r.status_code == 402
        assert r.json()["error"]["reason"] == "trial_expired"


def test_gated_route_returns_200_for_active_user(monkeypatch):
    app = _build_gated_test_app(monkeypatch, _make_profile("active"))
    with TestClient(app) as client:
        r = client.get("/test/gated")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "user_id": "test-user-123"}


def test_gated_route_returns_402_for_user_without_profile(monkeypatch):
    app = _build_gated_test_app(monkeypatch, None)
    with TestClient(app) as client:
        r = client.get("/test/gated")
        assert r.status_code == 402
        assert r.json()["error"]["reason"] == "free"
