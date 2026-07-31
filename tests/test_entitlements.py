"""Echo Vault entitlement guard + upload-quota enforcement.

Security-critical: non-entitled tiers must be blocked from mutations (403),
reads stay open, and over-quota uploads are rejected (507). The DynamoDB /
quota calls are mocked so no AWS is touched.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.app.core import entitlements as ent


def _req(method: str):
    return SimpleNamespace(method=method)


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["trial", "core", "core_plus"])
async def test_guard_allows_entitled_tiers(tier):
    profile = SimpleNamespace(subscription_tier=tier)
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
