"""Account deletion must actually delete the Cognito user.

Regression: the app authenticates with a Cognito ACCESS token, which carries no
`email` claim, so current_user["email"] was None. The old delete_account hit
`if user_email and user_id` as False, SKIPPED the Cognito + DynamoDB delete, and
STILL returned success — the client cleared its tokens while the account lived
on, so the user could log straight back in. Fix: resolve the email from the
users table by sub, and never report success when nothing was deleted.
"""

from types import SimpleNamespace
from typing import Tuple
from unittest.mock import AsyncMock

from src.app.controllers.auth_controller import AuthController


def _controller() -> Tuple[AuthController, AsyncMock, AsyncMock]:
    c = AuthController()
    cognito = AsyncMock()
    user_svc = AsyncMock()
    # setattr keeps mypy from type-checking the assignment against the real
    # service types; the returned mocks carry the assert_awaited* helpers.
    setattr(c, "cognito_service", cognito)
    setattr(c, "user_service", user_svc)
    return c, cognito, user_svc


async def test_delete_resolves_email_from_db_when_access_token_has_none():
    c, cognito, user_svc = _controller()
    # Access-token profile: only sub, no email.
    user_svc.get_user_profile.return_value = SimpleNamespace(email="real@example.com")

    result = await c.delete_account({"sub": "user-123"})

    assert result.success is True
    cognito.admin_delete_user.assert_awaited_once_with("real@example.com")
    user_svc.delete_user_account.assert_awaited_once_with("user-123")


async def test_delete_uses_email_claim_when_present():
    c, cognito, user_svc = _controller()

    result = await c.delete_account({"id": "user-1", "email": "a@b.com"})

    assert result.success is True
    cognito.admin_delete_user.assert_awaited_once_with("a@b.com")
    # No DB lookup needed when the token already carries the email.
    user_svc.get_user_profile.assert_not_awaited()


async def test_delete_returns_failure_when_email_unresolvable():
    c, cognito, user_svc = _controller()
    user_svc.get_user_profile.return_value = None

    result = await c.delete_account({"sub": "user-123"})

    # Must NOT report success — the client clears tokens on success, which would
    # strand a still-active account.
    assert result.success is False
    cognito.admin_delete_user.assert_not_awaited()
    user_svc.delete_user_account.assert_not_awaited()


async def test_delete_returns_failure_when_no_identifiers():
    c, cognito, _user_svc = _controller()

    result = await c.delete_account({})

    assert result.success is False
    cognito.admin_delete_user.assert_not_awaited()
