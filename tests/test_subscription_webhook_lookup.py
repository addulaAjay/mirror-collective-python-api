"""Apple webhook handlers must find the subscription by originalTransactionId.

Regression: subscriptions are stored keyed by the ORIGINAL transaction id
(stable), but the handlers previously looked them up by ``transactionId``, which
changes on every renewal. After the first renewal (every ~5 min in sandbox) the
lookup orphaned — so cancel / expiry / refund / renewal notifications never
mapped to the user and entitlements were never updated. These lock in that every
handler queries by originalTransactionId.
"""

from unittest.mock import AsyncMock

import pytest

from src.app.services.subscription_service import SubscriptionService

# A renewal-era notification: transactionId has rolled forward, but the stable
# key Apple always includes is originalTransactionId.
_RENEWED_TX = {
    "transactionId": "2000000000000009",  # latest renewal — changes each cycle
    "originalTransactionId": "1000000000000001",  # stable — the stored key
    "autoRenewStatus": False,
    "expiresDate": 1700000000000,
}

_HANDLERS = [
    "_handle_subscription_expired",
    "_handle_renewal_status_change",
    "_handle_refund",
    "_handle_subscription_renewal",
    "_handle_renewal_failure",
]


def _service_with_capture():
    """SubscriptionService whose query_items records its args and returns [] so
    each handler bails out right after the lookup (no full Subscription needed)."""
    db = AsyncMock()
    captured: list = []

    async def fake_query(**kwargs):
        captured.append(kwargs)
        return []

    db.query_items = AsyncMock(side_effect=fake_query)
    return SubscriptionService(db), captured


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", _HANDLERS)
async def test_handler_looks_up_by_original_transaction_id(handler):
    svc, captured = _service_with_capture()

    await getattr(svc, handler)(dict(_RENEWED_TX))

    assert captured, f"{handler} never queried the subscriptions table"
    sid = captured[0]["expression_values"][":sid"]
    assert sid == "1000000000000001", (
        f"{handler} looked up by the renewal transactionId ({sid}) instead of "
        "the stable originalTransactionId"
    )
    assert captured[0]["index_name"] == "subscription-id-index"


@pytest.mark.asyncio
async def test_falls_back_to_transaction_id_when_no_original():
    # Legacy/edge payloads without originalTransactionId still resolve.
    svc, captured = _service_with_capture()
    await svc._handle_subscription_expired({"transactionId": "1000000000000001"})
    assert captured[0]["expression_values"][":sid"] == "1000000000000001"


@pytest.mark.asyncio
async def test_renewal_clears_is_in_trial_on_paid_conversion():
    """When a trial converts to paid, DID_RENEW must reset is_in_trial=False so
    the user stops being classified as 'trial'. Regression: the handler only
    extended expiry + set ACTIVE, leaving the record's is_in_trial=True, so
    _update_user_subscription_status kept the profile at status 'trial'."""
    from src.app.models.subscription import (
        BillingPeriod,
        Platform,
        Subscription,
        SubscriptionStatus,
        SubscriptionType,
    )

    stored = Subscription(
        user_id="u1",
        subscription_id="1000000000000001",
        product_id="com.themirrorcollective.mirror.monthly",
        subscription_type=SubscriptionType.MIRROR_CORE,
        platform=Platform.IOS,
        status=SubscriptionStatus.ACTIVE,
        billing_period=BillingPeriod.MONTHLY,
        price_usd=0.0,
        purchase_date="2026-09-13T00:00:00Z",
        expiry_date="2026-09-27T00:00:00Z",
        auto_renew_enabled=True,
        receipt_data="r",
        is_in_trial=True,  # started as a trial
    )

    db = AsyncMock()
    db.query_items = AsyncMock(return_value=[stored.to_dynamodb_item()])
    saved: dict = {}
    db.put_item = AsyncMock(side_effect=lambda table, item: saved.update(item=item))
    svc = SubscriptionService(db)
    updated: dict = {}
    setattr(
        svc,
        "_update_user_subscription_status",
        AsyncMock(side_effect=lambda uid, sub: updated.update(sub=sub)),
    )

    # Paid renewal: no offerType (a genuine paid period, not intro/trial).
    await svc._handle_subscription_renewal(
        {
            "originalTransactionId": "1000000000000001",
            "transactionId": "2000000000000009",
            "expiresDate": 1793600000000,
        }
    )

    # Record no longer flags trial (the serializer omits the key when False).
    assert saved["item"].get("is_in_trial", False) is False
    # And the object handed to the profile updater is paid, so status → active.
    assert updated["sub"].is_in_trial is False
