"""Apple webhook DID_CHANGE_RENEWAL_STATUS handling.

Regression: ``autoRenewStatus`` lives in ``signedRenewalInfo``, not
``signedTransactionInfo`` — which handle_apple_webhook previously never decoded.
A cancellation was received, logged, and dropped: the record's
``auto_renew_enabled`` never flipped, and (with the active+auto_renew entitlement
bypass) a cancelled user kept access. Also guards the ``0 or ...`` falsy bug that
discarded auto-renew-OFF.
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

OTID = "1000000000000042"


def _stored(auto_renew=True):
    return Subscription(
        user_id="u1",
        subscription_id=OTID,
        product_id="com.themirrorcollective.mirror.yearly",
        subscription_type=SubscriptionType.MIRROR_CORE,
        platform=Platform.IOS,
        status=SubscriptionStatus.ACTIVE,
        billing_period=BillingPeriod.YEARLY,
        price_usd=89.0,
        purchase_date="2026-08-09T00:00:00Z",
        expiry_date="2026-09-21T00:00:00Z",
        auto_renew_enabled=auto_renew,
        receipt_data="r",
    )


def _service(stored, applied=True):
    db = AsyncMock()
    db.query_items = AsyncMock(
        return_value=[stored.to_dynamodb_item()] if stored else []
    )
    db.get_item = AsyncMock(return_value=None)
    # applied=False simulates a failed ConditionExpression (stale/out-of-order).
    db.update_item = AsyncMock(return_value=applied)
    db.put_item = AsyncMock()
    db.get_user_profile = AsyncMock(return_value=None)
    db.update_user_profile = AsyncMock()
    svc = SubscriptionService(db)
    return svc, db


def _update_values(db) -> dict:
    """ExpressionAttributeValues from the last update_item call (positional #4)."""
    call = db.update_item.await_args
    if call is None:
        return {}
    if len(call.args) > 3:
        return call.args[3]
    return call.kwargs.get("expression_values", {})


# ── Fix 2: the handler applies auto-renew OFF (0), instead of dropping it ──────
@pytest.mark.asyncio
async def test_handler_applies_auto_renew_off():
    """autoRenewStatus=0 (cancel) sets auto_renew_enabled=False via a targeted,
    ordered update_item (never a full put_item). Guards `0 or ...` too."""
    svc, db = _service(_stored(auto_renew=True))
    await svc._handle_renewal_status_change(
        {"originalTransactionId": OTID, "autoRenewStatus": 0, "signedDate": 1000}
    )
    assert _update_values(db)[":v0"] is False  # auto_renew_enabled set False


@pytest.mark.asyncio
async def test_handler_applies_auto_renew_on():
    svc, db = _service(_stored(auto_renew=False))
    await svc._handle_renewal_status_change(
        {"originalTransactionId": OTID, "autoRenewStatus": 1, "signedDate": 1000}
    )
    assert _update_values(db)[":v0"] is True  # auto_renew_enabled set True


@pytest.mark.asyncio
async def test_handler_orders_by_signed_date():
    """The write is conditional on signedDate so a stale/out-of-order event
    can't overwrite a newer one (Apple retries + reorders notifications)."""
    svc, db = _service(_stored(auto_renew=True))
    await svc._handle_renewal_status_change(
        {"originalTransactionId": OTID, "autoRenewStatus": 0, "signedDate": 9999}
    )
    call = db.update_item.await_args
    cond = call.kwargs.get("condition_expression", "")
    assert "last_notification_signed_date_ms" in cond  # ordered write
    assert _update_values(db)[":sd"] == 9999


# ── Fix 1: the webhook decodes signedRenewalInfo and merges autoRenewStatus ────
@pytest.mark.asyncio
async def test_webhook_merges_autorenew_from_renewal_info():
    """handle_apple_webhook must decode signedRenewalInfo and hand autoRenewStatus
    to the status-change handler — signedTransactionInfo never carries it."""
    svc, _ = _service(_stored())
    captured: dict = {}

    async def _capture(tx):
        captured.update(tx)

    svc._handle_renewal_status_change = _capture  # type: ignore[method-assign]
    svc._record_apple_notification_applied = AsyncMock()  # type: ignore[method-assign]
    svc._apple_notification_already_applied = AsyncMock(return_value=False)  # type: ignore[method-assign]

    decoded = {
        "notificationType": "DID_CHANGE_RENEWAL_STATUS",
        "notificationUUID": "uuid-1",
        "signedDate": 1,
        "data": {
            "signedTransactionInfo": "jws-tx",
            "signedRenewalInfo": "jws-renewal",
        },
    }
    tx = {"originalTransactionId": OTID, "transactionId": OTID, "signedDate": 1}
    renewal = {"autoRenewStatus": 0}  # cancelled
    with (
        patch(
            "src.app.services.subscription_service.verify_and_decode_apple_notification",
            return_value=(decoded, True),
        ),
        patch(
            "src.app.services.subscription_service.verify_apple_transaction_jws",
            return_value=tx,  # signedTransactionInfo
        ),
        patch(
            "src.app.services.subscription_service.verify_apple_renewal_info_jws",
            return_value=renewal,  # signedRenewalInfo (correct decoder)
        ),
    ):
        result = await svc.handle_apple_webhook({"signedPayload": "x"})

    assert result["success"] is True
    # autoRenewStatus from signedRenewalInfo reached the handler.
    assert captured.get("autoRenewStatus") == 0


@pytest.mark.asyncio
async def test_record_notification_uses_update_not_full_put():
    """_record_apple_notification_applied must use a targeted update_item — a
    full put_item would clobber a handler's just-applied change (e.g. a cancel)
    with a stale GSI read."""
    svc, _ = _service(_stored())
    await svc._record_apple_notification_applied(OTID, "uuid-x", 123456)
    svc.dynamodb_service.update_item.assert_awaited()
    svc.dynamodb_service.put_item.assert_not_awaited()


# ── Lifecycle handlers: renewal / expired / refund / fail all use the shared ──
# ── atomic, signedDate-ordered write and gate profile changes on `applied`. ───
from src.app.models.subscription import SubscriptionStatus  # noqa: E402


@pytest.mark.asyncio
async def test_renewal_sets_active_and_updates_profile_when_applied():
    svc, db = _service(_stored())
    svc._update_user_subscription_status = AsyncMock()  # type: ignore[method-assign]
    await svc._handle_subscription_renewal(
        {
            "originalTransactionId": OTID,
            "expiresDate": 1789931099000,
            "signedDate": 5000,
        }
    )
    vals = _update_values(db)
    assert SubscriptionStatus.ACTIVE.value in vals.values()
    assert vals[":sd"] == 5000  # ordered by signedDate
    svc._update_user_subscription_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_sets_expired_and_revokes_when_applied():
    svc, db = _service(_stored())
    svc._revoke_profile_access = AsyncMock()  # type: ignore[method-assign]
    await svc._handle_subscription_expired(
        {"originalTransactionId": OTID, "signedDate": 5000}
    )
    assert SubscriptionStatus.EXPIRED.value in _update_values(db).values()
    svc._revoke_profile_access.assert_awaited_once()


@pytest.mark.asyncio
async def test_refund_sets_refunded_and_revokes_when_applied():
    svc, db = _service(_stored())
    svc._revoke_profile_access = AsyncMock()  # type: ignore[method-assign]
    await svc._handle_refund({"originalTransactionId": OTID, "signedDate": 5000})
    assert SubscriptionStatus.REFUNDED.value in _update_values(db).values()
    svc._revoke_profile_access.assert_awaited_once()


@pytest.mark.asyncio
async def test_renewal_failure_sets_grace_period():
    svc, db = _service(_stored())
    await svc._handle_renewal_failure(
        {"originalTransactionId": OTID, "signedDate": 5000}
    )
    assert SubscriptionStatus.GRACE_PERIOD.value in _update_values(db).values()


@pytest.mark.asyncio
async def test_stale_expired_is_skipped_no_profile_downgrade():
    """A stale/out-of-order EXPIRED (condition fails → update_item False) must
    NOT revoke access — the newer state stands."""
    svc, db = _service(_stored(), applied=False)
    svc._revoke_profile_access = AsyncMock()  # type: ignore[method-assign]
    await svc._handle_subscription_expired(
        {"originalTransactionId": OTID, "signedDate": 1}
    )
    svc._revoke_profile_access.assert_not_awaited()
