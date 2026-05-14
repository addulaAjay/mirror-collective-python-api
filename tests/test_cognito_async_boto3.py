"""
Tests for the asyncio.to_thread wrapping around sync boto3 calls in
CognitoService.

These tests verify three properties:

1. The async wrapper actually awaits without raising
   "coroutine was never awaited" or similar.
2. The underlying boto3 client mock receives the right kwargs / positional
   args.
3. Concurrent invocations actually overlap on the threadpool — five
   concurrent calls that each sleep for 100ms should complete in roughly
   one sleep duration, not five.
"""

import asyncio
import os
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def cognito_service_with_mock_client():
    """Build a CognitoService whose boto3 client is a MagicMock."""
    os.environ.setdefault("COGNITO_USER_POOL_ID", "testpoolid123")
    os.environ.setdefault("COGNITO_CLIENT_ID", "testclientid123")
    os.environ.setdefault("AWS_REGION", "us-east-1")

    mock_client = MagicMock()
    with patch("boto3.client", return_value=mock_client):
        # Import inside the patch so __init__ uses the mocked boto3.client.
        from src.app.services.cognito_service import CognitoService

        service = CognitoService()
        # Re-bind in case the global conftest patch installed a shared mock.
        service.client = mock_client
        yield service, mock_client


@pytest.mark.asyncio
async def test_client_constructed_with_max_pool_connections():
    """boto3.client must receive a Config with max_pool_connections=50."""
    os.environ.setdefault("COGNITO_USER_POOL_ID", "testpoolid123")
    os.environ.setdefault("COGNITO_CLIENT_ID", "testclientid123")
    os.environ.setdefault("AWS_REGION", "us-east-1")

    with patch("src.app.services.cognito_service.boto3.client") as mock_boto3:
        mock_boto3.return_value = MagicMock()
        from src.app.services.cognito_service import CognitoService

        CognitoService()

    # First positional is service name "cognito-idp"; config kwarg must exist.
    args, kwargs = mock_boto3.call_args
    assert args[0] == "cognito-idp"
    assert "config" in kwargs
    config = kwargs["config"]
    # botocore.config.Config exposes the value as an attribute.
    assert config.max_pool_connections == 50
    # botocore.Config sets `retries` via __setattr__; not in the type stubs.
    assert config.retries == {"max_attempts": 5, "mode": "adaptive"}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sign_up_user_awaits_to_thread(cognito_service_with_mock_client):
    """sign_up_user must await the to_thread wrapper without warning."""
    service, mock_client = cognito_service_with_mock_client
    mock_client.sign_up.return_value = {
        "UserSub": "sub-123",
        "CodeDeliveryDetails": {"Destination": "u@example.com"},
        "UserConfirmed": False,
    }

    result = await service.sign_up_user(
        email="u@example.com",
        password="Pass!1234",
        first_name="U",
        last_name="One",
    )

    assert result["userSub"] == "sub-123"
    assert mock_client.sign_up.call_count == 1
    # Verify the kwargs that survived the to_thread bridge.
    kwargs = mock_client.sign_up.call_args.kwargs
    assert kwargs["ClientId"] == service.client_id
    assert kwargs["Username"] == "u@example.com"
    assert kwargs["Password"] == "Pass!1234"


@pytest.mark.asyncio
async def test_authenticate_user_passes_through_kwargs(
    cognito_service_with_mock_client,
):
    """authenticate_user must pass kwargs through to admin_initiate_auth."""
    service, mock_client = cognito_service_with_mock_client
    mock_client.admin_initiate_auth.return_value = {
        "AuthenticationResult": {
            "AccessToken": "a",
            "RefreshToken": "r",
            "IdToken": "i",
        }
    }

    result = await service.authenticate_user("u@example.com", "Pass!1234")

    assert result == {"accessToken": "a", "refreshToken": "r", "idToken": "i"}
    kwargs = mock_client.admin_initiate_auth.call_args.kwargs
    assert kwargs["UserPoolId"] == service.user_pool_id
    assert kwargs["ClientId"] == service.client_id
    assert kwargs["AuthFlow"] == "ADMIN_NO_SRP_AUTH"
    assert kwargs["AuthParameters"]["USERNAME"] == "u@example.com"


@pytest.mark.asyncio
async def test_get_user_wraps_in_to_thread(cognito_service_with_mock_client):
    """get_user must use to_thread (positional access_token kwarg)."""
    service, mock_client = cognito_service_with_mock_client
    mock_client.get_user.return_value = {
        "Username": "u@example.com",
        "UserAttributes": [{"Name": "email", "Value": "u@example.com"}],
        "Enabled": True,
        "UserStatus": "CONFIRMED",
    }

    result = await service.get_user("access-token-xyz")
    assert result["username"] == "u@example.com"
    assert mock_client.get_user.call_args.kwargs == {"AccessToken": "access-token-xyz"}


@pytest.mark.asyncio
async def test_admin_delete_user_passes_pool_and_username(
    cognito_service_with_mock_client,
):
    service, mock_client = cognito_service_with_mock_client
    mock_client.admin_delete_user.return_value = {}

    await service.admin_delete_user("u@example.com")
    kwargs = mock_client.admin_delete_user.call_args.kwargs
    assert kwargs == {
        "UserPoolId": service.user_pool_id,
        "Username": "u@example.com",
    }


@pytest.mark.asyncio
async def test_concurrent_calls_overlap_on_threadpool(
    cognito_service_with_mock_client,
):
    """
    Five concurrent get_user calls, each sleeping 100ms in the mock, must
    finish in roughly one sleep period (with generous slack for CI), proving
    the asyncio.to_thread wrappers actually overlap rather than serialize.
    """
    service, mock_client = cognito_service_with_mock_client
    sleep_ms = 100

    def slow_get_user(AccessToken):
        time.sleep(sleep_ms / 1000.0)
        return {
            "Username": AccessToken,
            "UserAttributes": [],
            "Enabled": True,
            "UserStatus": "CONFIRMED",
        }

    mock_client.get_user.side_effect = slow_get_user

    start = time.perf_counter()
    results = await asyncio.gather(*(service.get_user(f"token-{i}") for i in range(5)))
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert len(results) == 5
    assert all(r["username"] == f"token-{i}" for i, r in enumerate(results))
    # If calls serialized, elapsed would be ~5 * sleep_ms = 500ms.
    # With true overlap on the threadpool, elapsed should be ~sleep_ms.
    # Allow generous slack for CI scheduler jitter.
    assert elapsed_ms < sleep_ms * 3, (
        f"Calls did not overlap: elapsed={elapsed_ms:.1f}ms "
        f"(expected ~{sleep_ms}ms, serialized would be ~{5 * sleep_ms}ms)"
    )
