"""Apple webhook idempotency + ordering.

Apple retries App Store Server Notifications and does NOT guarantee delivery
order. Without a guard, a replayed REFUND or a stale EXPIRED arriving after a
newer DID_RENEW would clobber current entitlement state. handle_apple_webhook
records the last applied notification's UUID + signedDate on the subscription
and ignores anything already-applied or older.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.app.models.subscription import (
    BillingPeriod,
    Platform,
    Subscription,
    SubscriptionStatus,
    SubscriptionType,
)
from src.app.services.subscription_service import SubscriptionService

OTID = "1000000000000001"


def _stored(uuid=None, signed_ms=None):
    return Subscription(
        user_id="u1",
        subscription_id=OTID,
        product_id="com.themirrorcollective.mirror.monthly",
        subscription_type=SubscriptionType.MIRROR_CORE,
        platform=Platform.IOS,
        status=SubscriptionStatus.ACTIVE,
        billing_period=BillingPeriod.MONTHLY,
        price_usd=9.99,
        purchase_date="2026-09-13T00:00:00Z",
        expiry_date="2026-12-13T00:00:00Z",
        auto_renew_enabled=True,
        receipt_data="r",
        last_notification_uuid=uuid,
        last_notification_signed_date_ms=signed_ms,
    )


def _service(stored):
    db = AsyncMock()
    db.query_items = AsyncMock(
        return_value=[stored.to_dynamodb_item()] if stored else []
    )
    db.put_item = AsyncMock()
    return SubscriptionService(db)


def _payload(notification_type, uuid, signed_date):
    decoded = {
        "notificationType": notification_type,
        "notificationUUID": uuid,
        "signedDate": signed_date,
        "data": {"signedTransactionInfo": "jws"},
    }
    tx = {
        "originalTransactionId": OTID,
        "transactionId": OTID,
        "signedDate": signed_date,
    }
    return decoded, tx


async def _run(svc, decoded, tx):
    with (
        patch(
            "src.app.services.subscription_service.verify_and_decode_apple_notification",
            return_value=(decoded, True),
        ),
        patch(
            "src.app.services.subscription_service.verify_apple_transaction_jws",
            return_value=tx,
        ),
    ):
        return await svc.handle_apple_webhook({"signedPayload": "x"})


@pytest.mark.asyncio
async def test_replayed_notification_uuid_is_ignored():
    svc = _service(_stored(uuid="uuid-A", signed_ms=1000))
    handler = AsyncMock()
    setattr(svc, "_handle_refund", handler)
    decoded, tx = _payload("REFUND", "uuid-A", 1000)  # same UUID as stored

    result = await _run(svc, decoded, tx)

    assert "Ignored" in result["message"]
    handler.assert_not_awaited()  # the refund handler never ran


@pytest.mark.asyncio
async def test_out_of_order_older_notification_is_ignored():
    svc = _service(_stored(uuid="uuid-A", signed_ms=5000))
    handler = AsyncMock()
    setattr(svc, "_handle_subscription_expired", handler)
    decoded, tx = _payload("EXPIRED", "uuid-OLD", 2000)  # older than 5000

    result = await _run(svc, decoded, tx)

    assert "Ignored" in result["message"]
    handler.assert_not_awaited()  # stale EXPIRED did not revoke access


@pytest.mark.asyncio
async def test_newer_notification_is_processed_and_recorded():
    svc = _service(_stored(uuid="uuid-A", signed_ms=1000))
    handler = AsyncMock()
    setattr(svc, "_handle_subscription_renewal", handler)
    decoded, tx = _payload("DID_RENEW", "uuid-B", 9000)  # newer

    result = await _run(svc, decoded, tx)

    assert result["message"] == "Webhook processed"
    handler.assert_awaited_once()
    # Recording is now a targeted update_item (SET only the tracking fields) so
    # it can't clobber the handler's just-applied change with a stale GSI read.
    svc.dynamodb_service.update_item.assert_awaited()  # signedDate recorded


@pytest.mark.asyncio
async def test_first_ever_notification_is_processed():
    svc = _service(None)  # no stored subscription yet
    handler = AsyncMock()
    setattr(svc, "_handle_subscription_renewal", handler)
    decoded, tx = _payload("DID_RENEW", "uuid-1", 1000)

    result = await _run(svc, decoded, tx)

    assert result["message"] == "Webhook processed"
    handler.assert_awaited_once()
