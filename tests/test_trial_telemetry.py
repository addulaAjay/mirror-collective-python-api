"""
Trial-event telemetry (pricing spec 2026-05-12 §5).

Five events drive the trial conversion funnel:

  paywall_view    FE → POST /api/telemetry/paywall-view
  start_trial     BE → verify_and_activate_purchase (is_trial=True)
  trial_convert   BE → _handle_subscription_renewal (TRIAL → ACTIVE)
  trial_cancel    BE → cancel_subscription (while status == trial)
  trial_expire    BE → _handle_subscription_expired (was TRIAL before expire)

These tests pin the conditions so a future refactor can't quietly drop
a funnel signal. They use an in-memory emitter so we never depend on
the JSON-log sink's output format.
"""

from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest


class _RecordingEmitter:
    """In-memory TelemetryEmitter — captures every emit() for assertion."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def emit(self, event_name: str, *, user_hash: str, **fields: Any) -> None:
        self.events.append({"event": event_name, "user_hash": user_hash, **fields})

    def names(self) -> List[str]:
        return [e["event"] for e in self.events]


@pytest.fixture
def recording_emitter(monkeypatch):
    """Install a fresh recording emitter as the module-level default
    for the duration of one test. monkeypatch handles teardown so we
    can't leak the recording emitter into a sibling test.
    """
    from src.app.services.telemetry import reflection_events
    from src.app.services.telemetry import subscription_events as sub_events

    emitter = _RecordingEmitter()
    monkeypatch.setattr(reflection_events, "_default_emitter", emitter)
    # subscription_events imports get_default_emitter at call-time, so
    # patching the module-level _default_emitter in reflection_events is
    # enough; nothing in subscription_events caches it. Belt-and-braces:
    # also patch the re-exported name in subscription_events.
    monkeypatch.setattr(
        sub_events,
        "get_default_emitter",
        lambda: emitter,
        raising=False,
    )
    return emitter


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _build_service():
    from src.app.services.subscription_service import SubscriptionService

    return SubscriptionService(AsyncMock())


# --------------------------------------------------------------------------- #
# emit_subscription_event helper
# --------------------------------------------------------------------------- #


class TestEmitHelper:
    def test_hashes_user_id_before_emission(self, recording_emitter):
        from src.app.services.telemetry.subscription_events import (
            EVENT_START_TRIAL,
            emit_subscription_event,
        )

        emit_subscription_event(
            EVENT_START_TRIAL,
            user_id="user-1",
            subscription_id="sub-1",
            product_id="com.themirrorcollective.mirror.core.monthly",
            platform="ios",
        )

        assert len(recording_emitter.events) == 1
        ev = recording_emitter.events[0]
        assert ev["event"] == "start_trial"
        # user_hash must be the SHA-256 prefix, NOT the raw user_id.
        assert ev["user_hash"] != "user-1"
        assert len(ev["user_hash"]) == 32
        assert ev["subscription_id"] == "sub-1"
        assert ev["platform"] == "ios"

    def test_emitter_exception_is_swallowed(self, monkeypatch):
        """Telemetry must never break the subscription flow."""
        from src.app.services.telemetry import subscription_events as sub_events

        class _Boom:
            def emit(self, *args, **kwargs):
                raise RuntimeError("logger down")

        monkeypatch.setattr(sub_events, "get_default_emitter", lambda: _Boom())

        # Should NOT raise.
        sub_events.emit_subscription_event(
            sub_events.EVENT_START_TRIAL,
            user_id="user-1",
        )


# --------------------------------------------------------------------------- #
# start_trial — verify_and_activate_purchase, is_trial=True
# --------------------------------------------------------------------------- #


class TestStartTrialEmission:
    @pytest.mark.asyncio
    async def test_emits_when_purchase_is_a_trial(self, recording_emitter):
        from src.app.models.subscription import (
            BillingPeriod,
            Platform,
            Subscription,
            SubscriptionStatus,
            SubscriptionType,
        )

        svc = _build_service()
        # Stub the heavy validators / persistence so we can drive only
        # the post-activation code path.
        svc.receipt_validator.validate_apple_receipt = AsyncMock(
            return_value={
                "valid": True,
                "data": {
                    "original_transaction_id": "ot1",
                    "product_id": "com.themirrorcollective.mirror.core.monthly",
                    "is_trial_period": True,
                    "purchase_date": "2026-05-12T00:00:00Z",
                    "expiry_date": "2026-05-26T00:00:00Z",
                    "price": 0.0,
                },
            }
        )
        svc.dynamodb_service.get_item = AsyncMock(return_value=None)
        svc.dynamodb_service.put_item_if_not_exists = AsyncMock(return_value=True)
        svc.dynamodb_service.put_item = AsyncMock(return_value=None)
        svc._update_user_subscription_status = AsyncMock(return_value=None)
        svc._log_subscription_event = AsyncMock(return_value=None)

        await svc.verify_and_activate_purchase(
            user_id="user-1",
            platform="ios",
            receipt_data="<unused on ios>",
            product_id="com.themirrorcollective.mirror.core.monthly",
            transaction_id="ot1",
        )

        assert "start_trial" in recording_emitter.names()

    @pytest.mark.asyncio
    async def test_does_not_emit_for_non_trial_purchase(self, recording_emitter):
        svc = _build_service()
        svc.receipt_validator.validate_apple_receipt = AsyncMock(
            return_value={
                "valid": True,
                "data": {
                    "original_transaction_id": "ot1",
                    "product_id": "com.themirrorcollective.mirror.core.monthly",
                    "is_trial_period": False,
                    "purchase_date": "2026-05-12T00:00:00Z",
                    "expiry_date": "2026-06-12T00:00:00Z",
                    "price": 9.99,
                },
            }
        )
        svc.dynamodb_service.get_item = AsyncMock(return_value=None)
        svc.dynamodb_service.put_item_if_not_exists = AsyncMock(return_value=True)
        svc.dynamodb_service.put_item = AsyncMock(return_value=None)
        svc._update_user_subscription_status = AsyncMock(return_value=None)
        svc._log_subscription_event = AsyncMock(return_value=None)

        await svc.verify_and_activate_purchase(
            user_id="user-1",
            platform="ios",
            receipt_data="<unused>",
            product_id="com.themirrorcollective.mirror.core.monthly",
            transaction_id="ot1",
        )

        assert "start_trial" not in recording_emitter.names()


# --------------------------------------------------------------------------- #
# trial_convert — first paid renewal after trial
# --------------------------------------------------------------------------- #


class TestTrialConvertEmission:
    @pytest.mark.asyncio
    async def test_emits_when_renewal_flips_trial_to_active(self, recording_emitter):
        from src.app.models.subscription import (
            BillingPeriod,
            Platform,
            Subscription,
            SubscriptionStatus,
            SubscriptionType,
        )

        svc = _build_service()
        prior_trial = Subscription(
            user_id="user-1",
            subscription_id="ot1",
            product_id="com.themirrorcollective.mirror.core.monthly",
            subscription_type=SubscriptionType.MIRROR_BASIC,
            platform=Platform.IOS,
            status=SubscriptionStatus.TRIAL,
            billing_period=BillingPeriod.MONTHLY,
            price_usd=0.0,
        )
        svc._find_subscription_by_transaction_info = AsyncMock(return_value=prior_trial)
        svc.dynamodb_service.put_item = AsyncMock(return_value=None)
        svc._update_user_subscription_status = AsyncMock(return_value=None)
        svc._log_subscription_event = AsyncMock(return_value=None)

        await svc._handle_subscription_renewal({"transactionId": "t2"})

        assert "trial_convert" in recording_emitter.names()

    @pytest.mark.asyncio
    async def test_does_not_emit_on_normal_renewal(self, recording_emitter):
        from src.app.models.subscription import (
            BillingPeriod,
            Platform,
            Subscription,
            SubscriptionStatus,
            SubscriptionType,
        )

        svc = _build_service()
        prior_active = Subscription(
            user_id="user-1",
            subscription_id="ot1",
            product_id="com.themirrorcollective.mirror.core.monthly",
            subscription_type=SubscriptionType.MIRROR_BASIC,
            platform=Platform.IOS,
            status=SubscriptionStatus.ACTIVE,
            billing_period=BillingPeriod.MONTHLY,
            price_usd=9.99,
        )
        svc._find_subscription_by_transaction_info = AsyncMock(
            return_value=prior_active
        )
        svc.dynamodb_service.put_item = AsyncMock(return_value=None)
        svc._update_user_subscription_status = AsyncMock(return_value=None)
        svc._log_subscription_event = AsyncMock(return_value=None)

        await svc._handle_subscription_renewal({"transactionId": "t2"})

        assert "trial_convert" not in recording_emitter.names()


# --------------------------------------------------------------------------- #
# trial_cancel — user disables auto-renew while in trial
# --------------------------------------------------------------------------- #


class TestTrialCancelEmission:
    @pytest.mark.asyncio
    async def test_emits_when_cancelling_a_trial(self, recording_emitter):
        svc = _build_service()
        svc.dynamodb_service.get_item = AsyncMock(
            return_value={
                "user_id": "user-1",
                "subscription_id": "ot1",
                "product_id": "com.themirrorcollective.mirror.core.monthly",
                "platform": "ios",
                "expiry_date": "2026-05-26T00:00:00Z",
                "status": "trial",
            }
        )
        svc.dynamodb_service.update_item = AsyncMock(return_value=True)
        svc._log_subscription_event = AsyncMock(return_value=None)

        await svc.cancel_subscription(user_id="user-1", subscription_id="ot1")

        assert "trial_cancel" in recording_emitter.names()

    @pytest.mark.asyncio
    async def test_does_not_emit_when_cancelling_an_active_paid_sub(
        self, recording_emitter
    ):
        svc = _build_service()
        svc.dynamodb_service.get_item = AsyncMock(
            return_value={
                "user_id": "user-1",
                "subscription_id": "ot1",
                "product_id": "com.themirrorcollective.mirror.core.monthly",
                "platform": "ios",
                "expiry_date": "2026-06-12T00:00:00Z",
                "status": "active",
            }
        )
        svc.dynamodb_service.update_item = AsyncMock(return_value=True)
        svc._log_subscription_event = AsyncMock(return_value=None)

        await svc.cancel_subscription(user_id="user-1", subscription_id="ot1")

        assert "trial_cancel" not in recording_emitter.names()


# --------------------------------------------------------------------------- #
# trial_expire — trial runs out without conversion
# --------------------------------------------------------------------------- #


class TestTrialExpireEmission:
    @pytest.mark.asyncio
    async def test_emits_when_expiring_a_trial(self, recording_emitter):
        from src.app.models.subscription import (
            BillingPeriod,
            Platform,
            Subscription,
            SubscriptionStatus,
            SubscriptionType,
        )

        svc = _build_service()
        trial_sub = Subscription(
            user_id="user-1",
            subscription_id="ot1",
            product_id="com.themirrorcollective.mirror.core.monthly",
            subscription_type=SubscriptionType.MIRROR_BASIC,
            platform=Platform.IOS,
            status=SubscriptionStatus.TRIAL,
            billing_period=BillingPeriod.MONTHLY,
            price_usd=0.0,
        )
        svc._find_subscription_by_transaction_info = AsyncMock(return_value=trial_sub)
        svc.dynamodb_service.put_item = AsyncMock(return_value=None)
        svc.dynamodb_service.get_user_profile = AsyncMock(return_value=None)
        svc._log_subscription_event = AsyncMock(return_value=None)

        await svc._handle_subscription_expired({"transactionId": "t2"})

        assert "trial_expire" in recording_emitter.names()

    @pytest.mark.asyncio
    async def test_does_not_emit_when_expiring_a_paid_sub(self, recording_emitter):
        from src.app.models.subscription import (
            BillingPeriod,
            Platform,
            Subscription,
            SubscriptionStatus,
            SubscriptionType,
        )

        svc = _build_service()
        paid_sub = Subscription(
            user_id="user-1",
            subscription_id="ot1",
            product_id="com.themirrorcollective.mirror.core.monthly",
            subscription_type=SubscriptionType.MIRROR_BASIC,
            platform=Platform.IOS,
            status=SubscriptionStatus.ACTIVE,
            billing_period=BillingPeriod.MONTHLY,
            price_usd=9.99,
        )
        svc._find_subscription_by_transaction_info = AsyncMock(return_value=paid_sub)
        svc.dynamodb_service.put_item = AsyncMock(return_value=None)
        svc.dynamodb_service.get_user_profile = AsyncMock(return_value=None)
        svc._log_subscription_event = AsyncMock(return_value=None)

        await svc._handle_subscription_expired({"transactionId": "t2"})

        assert "trial_expire" not in recording_emitter.names()
