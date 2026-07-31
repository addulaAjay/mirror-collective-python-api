"""Unit tests for the /api/subscriptions/status public-subscription trimming.

The status endpoint must expose plan details (product, status, expiry,
auto-renew) but MUST NEVER leak receipt_data or raw platform payloads.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.app.api import subscription_routes as sr


@pytest.mark.asyncio
async def test_public_subscription_trims_receipt_and_internal_fields():
    item = {
        "subscription_id": "sub-1",
        "product_id": "com.themirrorcollective.mirror.core.monthly",
        "subscription_type": "mirror_core",
        "status": "active",
        "billing_period": "monthly",
        "expiry_date": "2026-09-01T00:00:00Z",
        "auto_renew_enabled": True,
        "is_in_trial": False,
        "receipt_data": "SECRET_RECEIPT_BLOB",
        "user_id": "u1",
        "events": [{"type": "purchase"}],
    }
    with patch.object(sr.dynamodb_service, "get_item", AsyncMock(return_value=item)):
        result = await sr._public_subscription_for("u1", "sub-1")

    assert result is not None
    # Security: no receipts or internal bookkeeping leak to the client.
    assert "receipt_data" not in result
    assert "user_id" not in result
    assert "events" not in result
    # Plan details are present.
    assert result["subscription_id"] == "sub-1"
    assert result["product_id"] == "com.themirrorcollective.mirror.core.monthly"
    assert result["status"] == "active"
    assert result["billing_period"] == "monthly"
    assert result["auto_renew_enabled"] is True


@pytest.mark.asyncio
async def test_public_subscription_none_when_no_id():
    result = await sr._public_subscription_for("u1", None)
    assert result is None


@pytest.mark.asyncio
async def test_public_subscription_none_when_not_found():
    with patch.object(sr.dynamodb_service, "get_item", AsyncMock(return_value=None)):
        result = await sr._public_subscription_for("u1", "missing")
    assert result is None
