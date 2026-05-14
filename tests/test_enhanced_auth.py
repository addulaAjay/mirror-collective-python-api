"""
Tests for :mod:`src.app.core.enhanced_auth`.

These tests verify the Wave 1B perf change that drops Cognito GetUser from
the per-request auth path:

  1. When the JWT already carries email/firstName/lastName, no Cognito
     call is issued.
  2. When the JWT lacks those fields, the first call hits Cognito and
     subsequent calls within the TTL window are served from cache.
  3. After the TTL elapses, the next call re-hits Cognito.
  4. If both the JWT short-circuit and Cognito GetUser fail, the
     fallback profile is returned (the endpoint never 500s on profile
     resolution).

We exercise ``get_user_with_profile`` directly as a coroutine with a
stubbed FastAPI ``Request`` and a mock ``CognitoService`` — this keeps
the tests focused on the new logic without spinning up the full app.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.app.core import enhanced_auth
from src.app.core.enhanced_auth import (
    _create_fallback_user,
    _reset_cache_for_tests,
    get_user_with_profile,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _make_request(authorization: str | None = "Bearer test-token") -> MagicMock:
    """Build a minimal stand-in for ``fastapi.Request`` with headers."""
    request = MagicMock()
    headers: Dict[str, str] = {}
    if authorization is not None:
        headers["authorization"] = authorization
    request.headers = headers
    return request


def _make_cognito_service(
    get_user_return: Dict[str, Any] | None = None,
    get_user_side_effect: Exception | None = None,
) -> MagicMock:
    """Build a mock CognitoService whose ``get_user`` is awaitable."""
    svc = MagicMock()
    if get_user_side_effect is not None:
        svc.get_user = AsyncMock(side_effect=get_user_side_effect)
    else:
        svc.get_user = AsyncMock(return_value=get_user_return)
    return svc


def _jwt_user_with_claims() -> Dict[str, Any]:
    """A current_user shaped like get_current_user output for an ID token."""
    return {
        "id": "sub-with-claims",
        "email": "person@example.com",
        "firstName": "Person",
        "lastName": "Example",
        "emailVerified": True,
        "provider": "cognito",
        "roles": ["basic_user"],
    }


def _jwt_user_access_token_only() -> Dict[str, Any]:
    """A current_user shaped like get_current_user output for an access token.

    Access tokens do not carry email/given_name/family_name in Cognito,
    so map_claims_to_profile produces empty strings for those fields.
    """
    return {
        "id": "sub-access-only",
        "email": None,
        "firstName": "",
        "lastName": "",
        "emailVerified": False,
        "provider": "cognito",
        "roles": ["basic_user"],
    }


def _cognito_get_user_payload() -> Dict[str, Any]:
    """A realistic Cognito GetUser response, post-conversion."""
    return {
        "username": "person-cognito-username",
        "userAttributes": {
            "email": "person@example.com",
            "given_name": "Person",
            "family_name": "Example",
            "email_verified": "true",
        },
        "enabled": True,
        "userStatus": "CONFIRMED",
    }


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    """Each test gets a clean module-level cache."""
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


# --------------------------------------------------------------------------- #
# Scenario 1: JWT claims short-circuit — no Cognito call
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_jwt_claims_short_circuit_skips_cognito():
    """If current_user has email + first/last name, we never call Cognito."""
    cognito = _make_cognito_service(get_user_return=_cognito_get_user_payload())
    request = _make_request()

    result = await get_user_with_profile(
        request=request,
        current_user=_jwt_user_with_claims(),
        cognito_service=cognito,
    )

    cognito.get_user.assert_not_awaited()
    assert result["email"] == "person@example.com"
    assert result["firstName"] == "Person"
    assert result["lastName"] == "Example"
    assert result["name"] == "Person Example"
    assert result["emailVerified"] is True
    # Pre-existing fields from current_user should still be present.
    assert result["roles"] == ["basic_user"]
    assert result["provider"] == "cognito"


# --------------------------------------------------------------------------- #
# Scenario 2: cache hit on the second call within TTL
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_second_call_within_ttl_is_served_from_cache(monkeypatch):
    """First call hits Cognito; second call within TTL must not."""
    # Keep the default TTL but make sure we don't rely on env state.
    monkeypatch.delenv("COGNITO_PROFILE_CACHE_TTL_SECONDS", raising=False)

    cognito = _make_cognito_service(get_user_return=_cognito_get_user_payload())
    request = _make_request()

    user = _jwt_user_access_token_only()

    first = await get_user_with_profile(
        request=request,
        current_user=user,
        cognito_service=cognito,
    )
    second = await get_user_with_profile(
        request=request,
        current_user=user,
        cognito_service=cognito,
    )

    assert cognito.get_user.await_count == 1, "Cognito should be hit exactly once"
    assert first == second
    assert first["email"] == "person@example.com"
    assert first["firstName"] == "Person"
    assert first["lastName"] == "Example"
    assert first["name"] == "Person Example"
    assert first["emailVerified"] is True
    assert first["cognitoUsername"] == "person-cognito-username"
    assert first["userStatus"] == "CONFIRMED"


# --------------------------------------------------------------------------- #
# Scenario 3: cache expiry — Cognito is re-hit after the TTL elapses
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cache_expiry_triggers_cognito_refetch(monkeypatch):
    """Once the TTL elapses, the next call must hit Cognito again."""
    # Fake monotonic clock under our control.
    clock = {"now": 1_000.0}

    def fake_monotonic() -> float:
        return clock["now"]

    monkeypatch.setattr(enhanced_auth.time, "monotonic", fake_monotonic)
    monkeypatch.setenv("COGNITO_PROFILE_CACHE_TTL_SECONDS", "60")

    cognito = _make_cognito_service(get_user_return=_cognito_get_user_payload())
    request = _make_request()
    user = _jwt_user_access_token_only()

    await get_user_with_profile(
        request=request,
        current_user=user,
        cognito_service=cognito,
    )
    assert cognito.get_user.await_count == 1

    # Advance just past the TTL.
    clock["now"] += 61.0

    await get_user_with_profile(
        request=request,
        current_user=user,
        cognito_service=cognito,
    )
    assert (
        cognito.get_user.await_count == 2
    ), "Cognito should be re-hit after TTL elapses"


# --------------------------------------------------------------------------- #
# Scenario 4: fallback when both JWT claims and Cognito are unavailable
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fallback_when_jwt_missing_and_cognito_raises():
    """If JWT claims are empty AND Cognito raises, we get the fallback."""
    cognito = _make_cognito_service(
        get_user_side_effect=RuntimeError("Cognito unreachable")
    )
    request = _make_request()
    user = _jwt_user_access_token_only()

    result = await get_user_with_profile(
        request=request,
        current_user=user,
        cognito_service=cognito,
    )

    assert cognito.get_user.await_count == 1
    expected_fallback = _create_fallback_user(user)
    assert result == expected_fallback
    assert result["firstName"] == ""
    assert result["lastName"] == ""
    assert result["emailVerified"] is False
    assert result["userStatus"] == "UNKNOWN"


# --------------------------------------------------------------------------- #
# Extra coverage: cache isolation between distinct subs
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cache_does_not_leak_between_users():
    """Two different subs must each get their own Cognito lookup + entry."""

    def _payload_for(email: str, given: str, family: str) -> Dict[str, Any]:
        return {
            "username": f"{given.lower()}-username",
            "userAttributes": {
                "email": email,
                "given_name": given,
                "family_name": family,
                "email_verified": "true",
            },
            "enabled": True,
            "userStatus": "CONFIRMED",
        }

    payloads: List[Dict[str, Any]] = [
        _payload_for("alice@example.com", "Alice", "A"),
        _payload_for("bob@example.com", "Bob", "B"),
    ]
    cognito = MagicMock()
    cognito.get_user = AsyncMock(side_effect=payloads)
    request = _make_request()

    user_a = {**_jwt_user_access_token_only(), "id": "sub-alice"}
    user_b = {**_jwt_user_access_token_only(), "id": "sub-bob"}

    res_a = await get_user_with_profile(
        request=request, current_user=user_a, cognito_service=cognito
    )
    res_b = await get_user_with_profile(
        request=request, current_user=user_b, cognito_service=cognito
    )

    assert cognito.get_user.await_count == 2
    assert res_a["email"] == "alice@example.com"
    assert res_b["email"] == "bob@example.com"

    # Second call for each user must reuse cache.
    res_a2 = await get_user_with_profile(
        request=request, current_user=user_a, cognito_service=cognito
    )
    res_b2 = await get_user_with_profile(
        request=request, current_user=user_b, cognito_service=cognito
    )
    assert cognito.get_user.await_count == 2
    assert res_a2["email"] == "alice@example.com"
    assert res_b2["email"] == "bob@example.com"


# --------------------------------------------------------------------------- #
# Sanity: running the module under plain ``asyncio.run`` works too.
# --------------------------------------------------------------------------- #
def test_sync_smoke_call() -> None:
    """Smoke test: invoking the coroutine via asyncio.run works end-to-end."""
    cognito = _make_cognito_service(get_user_return=_cognito_get_user_payload())
    request = _make_request()

    result = asyncio.run(
        get_user_with_profile(
            request=request,
            current_user=_jwt_user_with_claims(),
            cognito_service=cognito,
        )
    )
    assert result["name"] == "Person Example"
    cognito.get_user.assert_not_awaited()
