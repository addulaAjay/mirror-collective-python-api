"""SECRET_HASH on REFRESH_TOKEN_AUTH (Cognito refresh fix).

Production refresh failed with 401: "Client is configured with secret but
SECRET_HASH was not received". When the app client has a secret, Cognito
requires SECRET_HASH on REFRESH_TOKEN_AUTH, computed from the user's username.

Guardrails verified here:
  * SECRET_HASH is added only when both a client secret and a username exist.
  * The username is used ONLY to derive the HMAC (never logged, never used for
    authz — the refresh token stays the validated credential).
  * Neither the refresh token, the username, nor the SECRET_HASH is logged.
"""

import logging
from unittest.mock import AsyncMock, Mock

from src.app.api.models import RefreshTokenRequest
from src.app.controllers.auth_controller import AuthController
from src.app.services.cognito_service import CognitoService

_REFRESH_RESULT = {
    "AuthenticationResult": {
        "AccessToken": "new-access",
        "IdToken": "new-id",
        "RefreshToken": "new-refresh",
    }
}


def _service(client_secret):
    svc = CognitoService()
    svc.client_id = "test-client-id"
    svc.client_secret = client_secret
    svc.client = Mock()
    svc.client.initiate_auth = Mock(return_value=_REFRESH_RESULT)
    return svc


async def test_secret_hash_added_when_secret_and_username():
    svc = _service("topsecret")

    await svc.refresh_access_token("refresh-tok", username="user@example.com")

    params = svc.client.initiate_auth.call_args.kwargs
    auth_params = params["AuthParameters"]
    assert params["AuthFlow"] == "REFRESH_TOKEN_AUTH"
    assert auth_params["REFRESH_TOKEN"] == "refresh-tok"
    assert auth_params["SECRET_HASH"] == svc._get_secret_hash("user@example.com")


async def test_no_secret_hash_without_username():
    """Back-compat: no username -> no SECRET_HASH (e.g. a public client)."""
    svc = _service("topsecret")

    await svc.refresh_access_token("refresh-tok")

    auth_params = svc.client.initiate_auth.call_args.kwargs["AuthParameters"]
    assert "SECRET_HASH" not in auth_params


async def test_no_secret_hash_without_client_secret():
    svc = _service(None)

    await svc.refresh_access_token("refresh-tok", username="user@example.com")

    auth_params = svc.client.initiate_auth.call_args.kwargs["AuthParameters"]
    assert "SECRET_HASH" not in auth_params


async def test_refresh_does_not_log_secrets(caplog):
    svc = _service("topsecret")
    secret_hash = svc._get_secret_hash("user@example.com")

    with caplog.at_level(logging.DEBUG):
        await svc.refresh_access_token(
            "super-secret-refresh", username="user@example.com"
        )

    logs = " ".join(r.getMessage() for r in caplog.records)
    assert secret_hash not in logs
    assert "super-secret-refresh" not in logs
    assert "user@example.com" not in logs


async def test_controller_threads_username_through():
    controller = AuthController()
    controller.cognito_service = Mock()
    controller.cognito_service.refresh_access_token = AsyncMock(
        return_value={
            "accessToken": "a",
            "refreshToken": "r",
            "idToken": "i",
        }
    )

    await controller.refresh_token(
        RefreshTokenRequest(refreshToken="r", username="user@example.com")
    )

    controller.cognito_service.refresh_access_token.assert_awaited_once_with(
        "r", username="user@example.com"
    )


def test_refresh_request_username_is_optional():
    assert RefreshTokenRequest(refreshToken="r").username is None
    assert RefreshTokenRequest(refreshToken="r", username="u").username == "u"


def test_refresh_request_username_bounded_to_128_chars():
    import pytest
    from pydantic import ValidationError

    RefreshTokenRequest(refreshToken="r", username="x" * 128)  # ok
    with pytest.raises(ValidationError):
        RefreshTokenRequest(refreshToken="r", username="x" * 129)
