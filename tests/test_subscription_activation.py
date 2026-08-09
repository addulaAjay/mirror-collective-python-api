"""verify_and_activate_purchase must map modern App Store Server API fields.

Regression: the modern transaction payload returns dates as epoch-millis (*_ms)
and price in milliunits; activation previously read transaction_data["price"] /
["purchase_date"] / ["expiry_date"] and raised KeyError 'price' after JWS
verification finally started succeeding.
"""

from unittest.mock import AsyncMock

import pytest

from src.app.services.subscription_service import SubscriptionService, _ms_to_iso


def test_ms_to_iso_converts_and_handles_missing():
    assert _ms_to_iso(None) is None
    assert _ms_to_iso(0) is None
    iso = _ms_to_iso(1723204800000)
    assert iso is not None
    assert "T" in iso and iso.endswith("Z")


@pytest.mark.asyncio
async def test_verify_and_activate_maps_modern_apple_fields():
    db = AsyncMock()
    svc = SubscriptionService(db)
    svc.receipt_validator.validate_apple_receipt = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "valid": True,
            "data": {
                "transaction_id": "1000-orig",
                "original_transaction_id": "1000-orig",
                "product_id": "com.themirrorcollective.mirror.monthly",
                "purchase_date_ms": 1723204800000,
                "expires_date_ms": 1725796800000,
                "price": 9990,  # milliunits -> 9.99
                "auto_renew_status": True,
            },
        }
    )
    saved: dict = {}
    db.put_item = AsyncMock(side_effect=lambda table, item: saved.update(item=item))
    setattr(svc, "_update_user_subscription_status", AsyncMock())
    setattr(svc, "_log_subscription_event", AsyncMock())

    result = await svc.verify_and_activate_purchase(
        "u1",
        "ios",
        "receipt-data",
        "com.themirrorcollective.mirror.monthly",
        transaction_id="1000-orig",
    )

    assert result["success"] is True  # no KeyError 'price'
    item = saved["item"]
    assert item["price_usd"] == 9.99
    assert item["purchase_date"].endswith("Z")
    assert item["expiry_date"].endswith("Z")
    assert item["subscription_id"] == "1000-orig"


@pytest.mark.asyncio
async def test_verify_and_activate_defaults_price_when_absent():
    # Older payloads omit price -> 0.0, still no crash.
    db = AsyncMock()
    svc = SubscriptionService(db)
    svc.receipt_validator.validate_apple_receipt = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "valid": True,
            "data": {
                "transaction_id": "1000-orig",
                "original_transaction_id": "1000-orig",
                "product_id": "com.themirrorcollective.mirror.yearly",
                "purchase_date_ms": 1723204800000,
                "expires_date_ms": 1754740800000,
                "auto_renew_status": True,
            },
        }
    )
    saved: dict = {}
    db.put_item = AsyncMock(side_effect=lambda table, item: saved.update(item=item))
    setattr(svc, "_update_user_subscription_status", AsyncMock())
    setattr(svc, "_log_subscription_event", AsyncMock())

    result = await svc.verify_and_activate_purchase(
        "u1",
        "ios",
        "r",
        "com.themirrorcollective.mirror.yearly",
        transaction_id="1000-orig",
    )
    assert result["success"] is True
    assert saved["item"]["price_usd"] == 0.0
