"""Echo Vault entitlement guard + upload-quota enforcement.

Security-critical: non-entitled tiers must be blocked from mutations (403),
reads stay open, and over-quota uploads are rejected (507). The DynamoDB /
quota calls are mocked so no AWS is touched.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.app.core import entitlements as ent


def _req(method: str, path: str = "/api/echoes", body=None):
    """Fake Starlette Request. ``body`` (a dict) is returned by ``.json()``;
    None makes ``.json()`` raise, exercising the fail-closed path."""

    async def _json():
        if body is None:
            raise ValueError("no body")
        return body

    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        json=_json,
    )


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["trial", "core", "core_plus"])
async def test_guard_allows_entitled_tiers(tier):
    # A trial must still be within its window; paid tiers ignore the timestamp.
    profile = SimpleNamespace(
        subscription_tier=tier, trial_expires_at=_iso(timedelta(days=3))
    )
    with patch.object(
        ent._dynamodb, "get_user_profile", AsyncMock(return_value=profile)
    ):
        result = await ent.require_echo_vault_access(_req("POST"), {"id": "u1"})
    assert result == {"id": "u1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["free", "none", None, ""])
async def test_guard_blocks_non_entitled_tiers(tier):
    profile = SimpleNamespace(subscription_tier=tier)
    with patch.object(
        ent._dynamodb, "get_user_profile", AsyncMock(return_value=profile)
    ):
        with pytest.raises(HTTPException) as exc:
            await ent.require_echo_vault_access(_req("POST"), {"id": "u1"})
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "subscription_required"


@pytest.mark.asyncio
async def test_guard_blocks_expired_trial_before_cron_downgrades():
    # tier still reads "trial" (cron hasn't run) but the window has passed.
    profile = SimpleNamespace(
        subscription_tier="trial", trial_expires_at=_iso(timedelta(hours=-1))
    )
    with patch.object(
        ent._dynamodb, "get_user_profile", AsyncMock(return_value=profile)
    ):
        with pytest.raises(HTTPException) as exc:
            await ent.require_echo_vault_access(_req("POST"), {"id": "u1"})
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "subscription_required"


@pytest.mark.asyncio
async def test_guard_blocks_trial_with_missing_expiry_fail_closed():
    profile = SimpleNamespace(subscription_tier="trial", trial_expires_at=None)
    with patch.object(
        ent._dynamodb, "get_user_profile", AsyncMock(return_value=profile)
    ):
        with pytest.raises(HTTPException) as exc:
            await ent.require_echo_vault_access(_req("POST"), {"id": "u1"})
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_guard_allows_paid_tier_ignoring_trial_timestamp():
    # A paid subscriber may have a stale/expired trial_expires_at — must not block.
    profile = SimpleNamespace(
        subscription_tier="core", trial_expires_at=_iso(timedelta(days=-30))
    )
    with patch.object(
        ent._dynamodb, "get_user_profile", AsyncMock(return_value=profile)
    ):
        result = await ent.require_echo_vault_access(_req("POST"), {"id": "u1"})
    assert result == {"id": "u1"}


@pytest.mark.asyncio
async def test_guard_blocks_when_no_profile():
    with patch.object(ent._dynamodb, "get_user_profile", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await ent.require_echo_vault_access(_req("PATCH"), {"id": "u1"})
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_guard_allows_reads_without_checking_tier():
    with patch.object(ent._dynamodb, "get_user_profile", AsyncMock()) as gp:
        result = await ent.require_echo_vault_access(_req("GET"), {"id": "u1"})
    assert result == {"id": "u1"}
    gp.assert_not_called()  # reads never even fetch the profile


@pytest.mark.asyncio
@pytest.mark.parametrize("upload_type", ["profile", "user_profile"])
async def test_guard_allows_account_avatar_upload_without_entitlement(upload_type):
    # Account-level avatar upload on the upload-url route: an expired user must
    # still be able to change their own profile picture. No profile fetch.
    req = _req("POST", path="/api/echoes/upload-url", body={"upload_type": upload_type})
    with patch.object(ent._dynamodb, "get_user_profile", AsyncMock()) as gp:
        result = await ent.require_echo_vault_access(req, {"id": "u1"})
    assert result == {"id": "u1"}
    gp.assert_not_called()


@pytest.mark.asyncio
async def test_guard_still_gates_echo_upload_type_when_unentitled():
    # An "echo" upload on the same route is Echo Vault content → gated.
    req = _req("POST", path="/api/echoes/upload-url", body={"upload_type": "echo"})
    profile = SimpleNamespace(subscription_tier="free")
    with patch.object(
        ent._dynamodb, "get_user_profile", AsyncMock(return_value=profile)
    ):
        with pytest.raises(HTTPException) as exc:
            await ent.require_echo_vault_access(req, {"id": "u1"})
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_guard_gates_upload_url_with_unparseable_body_fail_closed():
    # Malformed/missing body on upload-url → treat as gated, not exempt.
    req = _req("POST", path="/api/echoes/upload-url", body=None)
    profile = SimpleNamespace(subscription_tier="free")
    with patch.object(
        ent._dynamodb, "get_user_profile", AsyncMock(return_value=profile)
    ):
        with pytest.raises(HTTPException) as exc:
            await ent.require_echo_vault_access(req, {"id": "u1"})
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_enforce_upload_quota_raises_when_over():
    with patch.object(
        ent._quota,
        "can_upload",
        AsyncMock(
            return_value={
                "can_upload": False,
                "reason": "quota_exceeded",
                "message": "full",
            }
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await ent.enforce_upload_quota("u1")
    assert exc.value.status_code == 507
    assert exc.value.detail["code"] == "quota_exceeded"


@pytest.mark.asyncio
async def test_enforce_upload_quota_passes_when_ok():
    with patch.object(
        ent._quota, "can_upload", AsyncMock(return_value={"can_upload": True})
    ):
        await ent.enforce_upload_quota("u1")  # must not raise


# --------------------------------------------------------------- end-to-end
# Prove the guard's body-peek coexists with the route's own body parsing:
# reading request.json() in the dependency must NOT consume the stream (Starlette
# caches it), so UploadUrlRequest still parses. Uses the REAL guard (not the
# conftest bypass) via a mini app.
@pytest.fixture()
def real_guard_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.app.api import echo_routes
    from src.app.core.error_handlers import setup_error_handlers
    from src.app.core.security import get_current_user

    app = FastAPI()
    app.include_router(echo_routes.router, prefix="/api")
    # Match production serialization so the 403 body carries error.code, the
    # exact shape the client's base.ts checks for.
    setup_error_handlers(app)

    async def _user():
        return {"id": "u1", "email": "u1@example.com"}

    app.dependency_overrides[get_current_user] = _user
    with TestClient(app) as c:
        yield c


def test_e2e_profile_upload_bypasses_guard_and_route_parses(real_guard_client):
    # Unentitled user: an account-level (profile) upload succeeds AND the route
    # body still parses after the guard peeked at it.
    free = SimpleNamespace(subscription_tier="free", trial_expires_at=None)
    with (
        patch.object(ent._dynamodb, "get_user_profile", AsyncMock(return_value=free)),
        patch.object(
            ent._quota, "can_upload", AsyncMock(return_value={"can_upload": True})
        ),
        patch.object(
            real_guard_client_module_echo_service(),
            "generate_upload_url",
            AsyncMock(return_value={"upload_url": "https://s3/put", "key": "k"}),
        ),
    ):
        r = real_guard_client.post(
            "/api/echoes/upload-url",
            json={"file_type": "image/jpeg", "upload_type": "profile"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True


def test_e2e_echo_upload_blocked_for_unentitled(real_guard_client):
    free = SimpleNamespace(subscription_tier="free", trial_expires_at=None)
    with patch.object(ent._dynamodb, "get_user_profile", AsyncMock(return_value=free)):
        r = real_guard_client.post(
            "/api/echoes/upload-url",
            json={"file_type": "image/jpeg", "upload_type": "echo"},
        )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "subscription_required"


def real_guard_client_module_echo_service():
    """The module-level echo_service singleton the route calls."""
    from src.app.api import echo_routes

    return echo_routes.echo_service
