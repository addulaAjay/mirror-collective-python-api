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
    db.get_item = AsyncMock(return_value=None)  # no prior record (#1 non-regress)
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
    db.get_item = AsyncMock(return_value=None)  # no prior record (#1 non-regress)
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


@pytest.mark.asyncio
async def test_verify_and_activate_marks_storekit_trial_as_trial():
    """A StoreKit intro/free-trial transaction must be recorded as a trial, not
    paid 'active'. Regression: is_in_trial was hardcoded False and the user
    profile status hardcoded 'active', so a trialing user was shown as fully
    paid (and the paywall offered 'MANAGE SUBSCRIPTION' instead of letting them
    convert)."""
    db = AsyncMock()
    svc = SubscriptionService(db)
    svc.receipt_validator.validate_apple_receipt = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "valid": True,
            "data": {
                "transaction_id": "2000-orig",
                "original_transaction_id": "2000-orig",
                "product_id": "com.themirrorcollective.mirror.monthly",
                "purchase_date_ms": 1723204800000,
                "expires_date_ms": 1724414400000,  # ~14 days later
                "price": 0,  # free trial → $0
                "is_trial_period": True,
                "auto_renew_status": True,
            },
        }
    )
    saved: dict = {}
    db.put_item = AsyncMock(side_effect=lambda table, item: saved.update(item=item))
    db.get_item = AsyncMock(return_value=None)  # no prior record (#1 non-regress)
    profile_update = AsyncMock()
    setattr(svc, "_update_user_subscription_status", profile_update)
    setattr(svc, "_log_subscription_event", AsyncMock())

    result = await svc.verify_and_activate_purchase(
        "u1",
        "ios",
        "receipt-data",
        "com.themirrorcollective.mirror.monthly",
        transaction_id="2000-orig",
    )

    assert result["success"] is True
    assert saved["item"]["is_in_trial"] is True
    assert saved["item"]["price_usd"] == 0.0
    # The subscription handed to the profile updater is flagged as a trial.
    sub_arg = profile_update.call_args.args[1]
    assert sub_arg.is_in_trial is True


@pytest.mark.asyncio
async def test_restore_maps_modern_fields_and_counts():
    """Restore must normalise *_ms dates + milliunits price like the purchase
    path. Regression: it read transaction_data['purchase_date']/['expiry_date']
    /['price'] (keys that don't exist) → KeyError swallowed → restored_count 0,
    so a paying user reinstalling could never regain access."""
    db = AsyncMock()
    svc = SubscriptionService(db)
    svc.receipt_validator.validate_apple_receipt = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "valid": True,
            "data": {
                "transaction_id": "3000-orig",
                "original_transaction_id": "3000-orig",
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
    db.get_item = AsyncMock(return_value=None)  # no prior record (#1 non-regress)
    setattr(svc, "_update_user_subscription_status", AsyncMock())
    setattr(svc, "_log_subscription_event", AsyncMock())

    result = await svc.restore_user_purchases("u1", "ios", ["receipt-blob"])

    assert result["success"] is True
    assert result["restored_count"] == 1  # no KeyError, sub actually restored
    item = saved["item"]
    assert item["price_usd"] == 9.99  # milliunits divided
    assert item["purchase_date"].endswith("Z")  # *_ms -> ISO
    assert item["expiry_date"].endswith("Z")
    assert item["subscription_id"] == "3000-orig"


@pytest.mark.asyncio
async def test_verify_prefers_latest_expiry_from_subscription_status():
    """#2 — the single-transaction lookup can report the ORIGINAL period; the
    persisted expiry must be advanced to Apple's latest subscription-status
    expiry when that's later."""
    db = AsyncMock()
    svc = SubscriptionService(db)
    stale_ms = 1723204800000  # 2024-08-09 (original period)
    latest_ms = 1754740800000  # 2025-08-09 (current renewal)
    svc.receipt_validator.validate_apple_receipt = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "valid": True,
            "data": {
                "transaction_id": "1000-orig",
                "original_transaction_id": "1000-orig",
                "product_id": "com.themirrorcollective.mirror.yearly",
                "purchase_date_ms": 1723204800000,
                "expires_date_ms": stale_ms,
                "auto_renew_status": True,
            },
        }
    )
    svc.receipt_validator.get_apple_latest_expiry_ms = AsyncMock(  # type: ignore[method-assign]
        return_value=latest_ms
    )
    db.get_item = AsyncMock(return_value=None)
    saved: dict = {}
    db.put_item = AsyncMock(side_effect=lambda table, item: saved.update(item=item))
    setattr(svc, "_update_user_subscription_status", AsyncMock())
    setattr(svc, "_log_subscription_event", AsyncMock())

    await svc.verify_and_activate_purchase(
        "u1",
        "ios",
        "r",
        "com.themirrorcollective.mirror.yearly",
        transaction_id="1000-orig",
    )
    assert saved["item"]["expiry_date"] == _ms_to_iso(latest_ms)


@pytest.mark.asyncio
async def test_verify_never_regresses_stored_expiry():
    """#1 — a stale incoming expiry must not overwrite a later expiry already on
    the stored record."""
    db = AsyncMock()
    svc = SubscriptionService(db)
    stale_ms = 1723204800000  # 2024-08-09
    stored_iso = _ms_to_iso(1754740800000)  # 2025-08-09, already recorded
    svc.receipt_validator.validate_apple_receipt = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "valid": True,
            "data": {
                "transaction_id": "1000-orig",
                "original_transaction_id": "1000-orig",
                "product_id": "com.themirrorcollective.mirror.yearly",
                "purchase_date_ms": 1723204800000,
                "expires_date_ms": stale_ms,
                "auto_renew_status": True,
            },
        }
    )
    svc.receipt_validator.get_apple_latest_expiry_ms = AsyncMock(return_value=None)  # type: ignore[method-assign]
    db.get_item = AsyncMock(return_value={"expiry_date": stored_iso})
    saved: dict = {}
    db.put_item = AsyncMock(side_effect=lambda table, item: saved.update(item=item))
    setattr(svc, "_update_user_subscription_status", AsyncMock())
    setattr(svc, "_log_subscription_event", AsyncMock())

    await svc.verify_and_activate_purchase(
        "u1",
        "ios",
        "r",
        "com.themirrorcollective.mirror.yearly",
        transaction_id="1000-orig",
    )
    assert saved["item"]["expiry_date"] == stored_iso  # kept the later value
