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


def _req(method: str):
    return SimpleNamespace(method=method)


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
