"""
Phase A — IAP receipt-security tests.

Covers:
  - apple_app_store_client config-error paths and the verify wrappers.
  - receipt_validator delegates to the Apple SDK correctly and surfaces
    structured error responses.
  - handle_apple_webhook rejects unverified ASSN v2 payloads with 401.
  - handle_google_webhook rejects missing / forged Pub/Sub OIDC JWTs.
  - verify_and_activate_purchase is idempotent — repeated calls for
    the same transaction_id return the existing record without
    re-firing SUBSCRIPTION_PURCHASED.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# --------------------------------------------------------------------------- #
# AppleAppStoreClient — config errors
# --------------------------------------------------------------------------- #


class TestAppleAppStoreClientConfig:
    def setup_method(self):
        """Each test must start with a fresh client cache."""
        from src.app.services.apple_app_store_client import reset_clients_for_testing

        reset_clients_for_testing()

    def teardown_method(self):
        from src.app.services.apple_app_store_client import reset_clients_for_testing

        reset_clients_for_testing()

    def test_verifier_requires_bundle_id(self, monkeypatch):
        from src.app.services.apple_app_store_client import (
            AppleClientConfigError,
            _get_signed_data_verifier,
        )

        monkeypatch.delenv("APPLE_BUNDLE_ID", raising=False)

        with pytest.raises(AppleClientConfigError) as exc:
            _get_signed_data_verifier()

        assert "APPLE_BUNDLE_ID" in str(exc.value)

    def test_api_client_requires_all_credentials(self, monkeypatch):
        from src.app.services.apple_app_store_client import (
            AppleClientConfigError,
            _get_api_client,
        )

        # Set bundle_id but leave issuer/key/private key missing.
        monkeypatch.setenv("APPLE_BUNDLE_ID", "com.example.app")
        monkeypatch.delenv("APPLE_ISSUER_ID", raising=False)
        monkeypatch.delenv("APPLE_KEY_ID", raising=False)
        monkeypatch.delenv("APPLE_PRIVATE_KEY", raising=False)

        with pytest.raises(AppleClientConfigError) as exc:
            _get_api_client()

        msg = str(exc.value)
        for var in ("APPLE_ISSUER_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY"):
            assert var in msg

    def test_sandbox_env_toggle(self, monkeypatch):
        from appstoreserverlibrary.models.Environment import Environment

        from src.app.services.apple_app_store_client import _resolve_environment

        monkeypatch.setenv("APPLE_USE_SANDBOX", "true")
        assert _resolve_environment() == Environment.SANDBOX

        monkeypatch.setenv("APPLE_USE_SANDBOX", "false")
        assert _resolve_environment() == Environment.PRODUCTION


# --------------------------------------------------------------------------- #
# AppleAppStoreClient — verify wrappers (mocked SDK)
# --------------------------------------------------------------------------- #


class TestVerifySignedTransaction:
    def setup_method(self):
        from src.app.services.apple_app_store_client import reset_clients_for_testing

        reset_clients_for_testing()

    def test_signature_failure_raises(self, monkeypatch):
        from appstoreserverlibrary.signed_data_verifier import (
            VerificationException,
            VerificationStatus,
        )

        from src.app.services import apple_app_store_client

        fake_verifier = MagicMock()
        fake_verifier.verify_and_decode_signed_transaction.side_effect = (
            VerificationException(VerificationStatus.INVALID_CHAIN)
        )
        monkeypatch.setattr(
            apple_app_store_client,
            "_get_signed_data_verifier",
            lambda: fake_verifier,
        )

        from src.app.services.apple_app_store_client import (
            AppleSignatureVerificationError,
            verify_signed_transaction,
        )

        with pytest.raises(AppleSignatureVerificationError):
            verify_signed_transaction("eyJ...")

    def test_signature_success_returns_dict(self, monkeypatch):
        from dataclasses import dataclass

        from src.app.services import apple_app_store_client

        @dataclass
        class FakePayload:
            transactionId: str
            originalTransactionId: str
            productId: str

        fake_verifier = MagicMock()
        fake_verifier.verify_and_decode_signed_transaction.return_value = FakePayload(
            transactionId="t1",
            originalTransactionId="ot1",
            productId="com.themirrorcollective.mirror.core.monthly",
        )
        monkeypatch.setattr(
            apple_app_store_client,
            "_get_signed_data_verifier",
            lambda: fake_verifier,
        )

        from src.app.services.apple_app_store_client import verify_signed_transaction

        result = verify_signed_transaction("eyJ...")
        assert result["transactionId"] == "t1"
        assert result["originalTransactionId"] == "ot1"


# --------------------------------------------------------------------------- #
# receipt_validator.validate_apple_receipt
# --------------------------------------------------------------------------- #


class TestReceiptValidatorApple:
    @pytest.mark.asyncio
    async def test_requires_transaction_id(self):
        from src.app.services.receipt_validator import ReceiptValidator

        validator = ReceiptValidator()
        result = await validator.validate_apple_receipt(
            receipt_data="", original_transaction_id=None
        )
        assert result["valid"] is False
        assert "original_transaction_id" in result["error"]

    @pytest.mark.asyncio
    async def test_signature_failure_returns_error(self, monkeypatch):
        from src.app.services import apple_app_store_client
        from src.app.services.apple_app_store_client import (
            AppleSignatureVerificationError,
        )

        def boom(*_args, **_kwargs):
            raise AppleSignatureVerificationError("x5c chain invalid")

        monkeypatch.setattr(apple_app_store_client, "get_subscription_statuses", boom)

        from src.app.services.receipt_validator import ReceiptValidator

        validator = ReceiptValidator()
        result = await validator.validate_apple_receipt(
            receipt_data="", original_transaction_id="ot1"
        )
        assert result["valid"] is False
        assert "signature" in result["error"].lower()

    def test_auto_renew_derived_from_signed_renewal_info(self):
        """parse_apple_signed_transaction must read autoRenewStatus
        from the JWS-verified renewal info (attached under
        `_renewal_info` by apple_app_store_client) instead of
        hardcoding True."""
        from src.app.services.receipt_validator import ReceiptValidator

        v = ReceiptValidator()

        # Off
        parsed = v.parse_apple_signed_transaction(
            {
                "transactionId": "t1",
                "originalTransactionId": "ot1",
                "productId": "com.themirrorcollective.mirror.core.monthly",
                "_renewal_info": {"autoRenewStatus": 0},
            }
        )
        assert parsed["auto_renew_enabled"] is False

        # On
        parsed = v.parse_apple_signed_transaction(
            {
                "transactionId": "t2",
                "originalTransactionId": "ot1",
                "productId": "com.themirrorcollective.mirror.core.monthly",
                "_renewal_info": {"autoRenewStatus": 1},
            }
        )
        assert parsed["auto_renew_enabled"] is True

        # Missing renewal info → default True for backwards-compat with
        # migration-window transactions that don't carry it.
        parsed = v.parse_apple_signed_transaction(
            {
                "transactionId": "t3",
                "originalTransactionId": "ot1",
                "productId": "com.themirrorcollective.mirror.core.monthly",
            }
        )
        assert parsed["auto_renew_enabled"] is True

    @pytest.mark.asyncio
    async def test_happy_path_returns_parsed_transaction(self, monkeypatch):
        from src.app.services import apple_app_store_client

        latest = {
            "transactionId": "t1",
            "originalTransactionId": "ot1",
            "productId": "com.themirrorcollective.mirror.core.monthly",
            "purchaseDate": 1_700_000_000_000,
            "expiresDate": 1_730_000_000_000,
            "price": 15990000,  # micros -> $15.99
            "environment": "Production",
            "bundleId": "com.themirrorcollective.mirror",
        }

        def fake_status(_txn_id):
            return {
                "environment": "Production",
                "signed_transactions": [latest],
                "latest_signed_transaction": latest,
            }

        monkeypatch.setattr(
            apple_app_store_client, "get_subscription_statuses", fake_status
        )

        from src.app.services.receipt_validator import ReceiptValidator

        validator = ReceiptValidator()
        result = await validator.validate_apple_receipt(
            receipt_data="", original_transaction_id="ot1"
        )

        assert result["valid"] is True
        data = result["data"]
        assert data["transaction_id"] == "t1"
        assert data["original_transaction_id"] == "ot1"
        assert data["product_id"] == ("com.themirrorcollective.mirror.core.monthly")
        # Micros price converted to USD float.
        assert data["price"] == pytest.approx(15.99, rel=1e-3)
        # Timestamps converted to ISO 8601 UTC.
        assert data["purchase_date"].endswith("Z")
        assert data["expiry_date"].endswith("Z")


# --------------------------------------------------------------------------- #
# handle_apple_webhook — ASSN v2 signature verification
# --------------------------------------------------------------------------- #


def _build_subscription_service(monkeypatch):
    """Build a SubscriptionService with no-op dependencies for unit tests."""
    from src.app.services.subscription_service import SubscriptionService

    dynamodb = MagicMock()
    dynamodb.get_item = AsyncMock(return_value=None)
    dynamodb.put_item = AsyncMock(return_value=None)
    # Default: conditional put succeeds (no existing row, no race).
    # Tests that exercise the race path override this on the mock.
    dynamodb.put_item_if_not_exists = AsyncMock(return_value=True)
    dynamodb.update_user_profile = AsyncMock(return_value=None)
    dynamodb.get_user_profile = AsyncMock(return_value=None)

    svc = SubscriptionService(dynamodb)
    return svc, dynamodb


class TestAppleWebhookSignature:
    @pytest.mark.asyncio
    async def test_missing_signed_payload_returns_400(self, monkeypatch):
        svc, _ = _build_subscription_service(monkeypatch)
        result = await svc.handle_apple_webhook({})
        assert result["success"] is False
        assert result["status_code"] == 400

    @pytest.mark.asyncio
    async def test_bad_signature_returns_401(self, monkeypatch):
        svc, _ = _build_subscription_service(monkeypatch)

        from src.app.services import apple_app_store_client
        from src.app.services.apple_app_store_client import (
            AppleSignatureVerificationError,
        )

        def boom(*_args, **_kwargs):
            raise AppleSignatureVerificationError("forged")

        monkeypatch.setattr(apple_app_store_client, "verify_signed_notification", boom)

        result = await svc.handle_apple_webhook({"signedPayload": "forged"})
        assert result["success"] is False
        assert result["status_code"] == 401

    @pytest.mark.asyncio
    async def test_valid_payload_dispatches_renewal_handler(self, monkeypatch):
        svc, _ = _build_subscription_service(monkeypatch)

        from src.app.services import apple_app_store_client

        signed_tx = {
            "transactionId": "t2",
            "originalTransactionId": "ot1",
            "productId": "com.themirrorcollective.mirror.core.monthly",
            "purchaseDate": 1_700_000_000_000,
            "expiresDate": 1_730_000_000_000,
        }
        monkeypatch.setattr(
            apple_app_store_client,
            "verify_signed_notification",
            lambda _: {
                "notificationType": "DID_RENEW",
                "data": {"signedTransactionInfo": "<inner JWS>"},
            },
        )
        monkeypatch.setattr(
            apple_app_store_client,
            "verify_signed_transaction",
            lambda _: signed_tx,
        )

        renew = AsyncMock()
        svc._handle_subscription_renewal = renew  # type: ignore[assignment]

        result = await svc.handle_apple_webhook({"signedPayload": "<JWS>"})

        assert result["success"] is True
        renew.assert_awaited_once_with(signed_tx)


# --------------------------------------------------------------------------- #
# handle_google_webhook — Pub/Sub OIDC JWT verification
# --------------------------------------------------------------------------- #


class TestGoogleWebhookJWT:
    @pytest.mark.asyncio
    async def test_missing_auth_header_returns_401(self, monkeypatch):
        # Ensure verification is ON.
        monkeypatch.setenv("GOOGLE_PUBSUB_VERIFY", "true")
        monkeypatch.setenv("GOOGLE_PUBSUB_AUDIENCE", "https://example.com/hook")
        monkeypatch.setenv(
            "GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL",
            "play-rtdn@example.iam.gserviceaccount.com",
        )

        svc, _ = _build_subscription_service(monkeypatch)
        result = await svc.handle_google_webhook(
            {"message": {"data": "dGVzdA=="}}, auth_header=None
        )
        assert result["success"] is False
        assert result["status_code"] == 401

    @pytest.mark.asyncio
    async def test_email_mismatch_returns_401(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_PUBSUB_VERIFY", "true")
        monkeypatch.setenv("GOOGLE_PUBSUB_AUDIENCE", "https://example.com/hook")
        monkeypatch.setenv(
            "GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL",
            "play-rtdn@example.iam.gserviceaccount.com",
        )

        svc, _ = _build_subscription_service(monkeypatch)

        with patch(
            "google.oauth2.id_token.verify_oauth2_token",
            return_value={
                "email": "attacker@elsewhere.iam.gserviceaccount.com",
                "email_verified": True,
            },
        ):
            result = await svc.handle_google_webhook(
                {"message": {"data": "dGVzdA=="}},
                auth_header="Bearer some.token.here",
            )
        assert result["success"] is False
        assert result["status_code"] == 401

    @pytest.mark.asyncio
    async def test_valid_jwt_proceeds_to_message_decode(self, monkeypatch):
        import base64
        import json

        monkeypatch.setenv("GOOGLE_PUBSUB_VERIFY", "true")
        monkeypatch.setenv("GOOGLE_PUBSUB_AUDIENCE", "https://example.com/hook")
        monkeypatch.setenv(
            "GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL",
            "play-rtdn@example.iam.gserviceaccount.com",
        )

        svc, _ = _build_subscription_service(monkeypatch)

        # Encode a benign notification body — type=4 (PURCHASED) which
        # only logs; no downstream side effects to mock out.
        body = base64.b64encode(
            json.dumps(
                {
                    "subscriptionNotification": {
                        "notificationType": 4,
                        "purchaseToken": "pt",
                        "subscriptionId": (
                            "com.themirrorcollective.mirror.core.monthly"
                        ),
                    }
                }
            ).encode()
        ).decode()

        with patch(
            "google.oauth2.id_token.verify_oauth2_token",
            return_value={
                "email": "play-rtdn@example.iam.gserviceaccount.com",
                "email_verified": True,
                "aud": "https://example.com/hook",
            },
        ):
            result = await svc.handle_google_webhook(
                {"message": {"data": body}},
                auth_header="Bearer some.token.here",
            )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_verify_disabled_short_circuit(self, monkeypatch):
        """GOOGLE_PUBSUB_VERIFY=false bypasses the JWT check (dev only)."""
        monkeypatch.setenv("GOOGLE_PUBSUB_VERIFY", "false")
        svc, _ = _build_subscription_service(monkeypatch)

        result = await svc.handle_google_webhook(
            {"message": {"data": "dGVzdA=="}},  # base64("test")
            auth_header=None,
        )
        # We bypassed JWT verify; the next step (decode "test" as JSON)
        # fails, so we expect a 400 — NOT a 401.
        assert result["success"] is False
        assert result["status_code"] != 401


# --------------------------------------------------------------------------- #
# verify_and_activate_purchase — idempotency + product whitelist
# --------------------------------------------------------------------------- #


class TestVerifyPurchaseIdempotency:
    """Calls /verify-purchase end-to-end through the service with mocked
    receipt validation + DynamoDB layer."""

    @pytest.fixture
    def parsed_transaction(self):
        return {
            "transaction_id": "t1",
            "original_transaction_id": "ot1",
            "product_id": "com.themirrorcollective.mirror.core.monthly",
            "purchase_date": "2026-01-01T00:00:00Z",
            "expiry_date": "2026-02-01T00:00:00Z",
            "is_trial_period": False,
            "is_in_intro_offer_period": False,
            "auto_renew_enabled": True,
            "price": 15.99,
            "currency_code": "USD",
            "environment": "Production",
        }

    @pytest.mark.asyncio
    async def test_first_call_creates_subscription(
        self, monkeypatch, parsed_transaction
    ):
        svc, dynamodb = _build_subscription_service(monkeypatch)

        svc.receipt_validator.validate_apple_receipt = AsyncMock(
            return_value={"valid": True, "data": parsed_transaction, "error": None}
        )
        svc._update_user_subscription_status = AsyncMock(return_value=None)
        svc._log_subscription_event = AsyncMock(return_value=None)

        result = await svc.verify_and_activate_purchase(
            user_id="u1",
            platform="ios",
            receipt_data="legacy",
            product_id="com.themirrorcollective.mirror.core.monthly",
            transaction_id="ot1",
        )

        assert result["success"] is True
        assert result["idempotent"] is False
        # The subscription row was written via the atomic conditional put.
        assert dynamodb.put_item_if_not_exists.await_count == 1
        # ...and the SUBSCRIPTION_PURCHASED event was fired exactly once.
        svc._log_subscription_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_repeated_call_returns_existing_without_re_event(
        self, monkeypatch, parsed_transaction
    ):
        svc, dynamodb = _build_subscription_service(monkeypatch)

        svc.receipt_validator.validate_apple_receipt = AsyncMock(
            return_value={"valid": True, "data": parsed_transaction, "error": None}
        )

        # Simulate an already-existing subscription row.
        existing = {
            "user_id": "u1",
            "subscription_id": "ot1",
            "product_id": "com.themirrorcollective.mirror.core.monthly",
            "status": "ACTIVE",
        }
        dynamodb.get_item = AsyncMock(return_value=existing)

        result = await svc.verify_and_activate_purchase(
            user_id="u1",
            platform="ios",
            receipt_data="legacy",
            product_id="com.themirrorcollective.mirror.core.monthly",
            transaction_id="ot1",
        )

        assert result["success"] is True
        assert result["idempotent"] is True
        assert result["subscription"] == existing
        # Critically: we DID NOT put_item / DID NOT fire a duplicate
        # SUBSCRIPTION_PURCHASED event.
        dynamodb.put_item.assert_not_awaited()
        dynamodb.put_item_if_not_exists.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_sku_rejected(self, monkeypatch):
        svc, _ = _build_subscription_service(monkeypatch)

        # No need to mock validate_apple_receipt — the whitelist check
        # happens before any platform call.
        with pytest.raises(ValueError) as exc:
            await svc.verify_and_activate_purchase(
                user_id="u1",
                platform="ios",
                receipt_data="legacy",
                product_id="com.attacker.fakeproduct",
                transaction_id="ot1",
            )
        assert "Unknown product_id" in str(exc.value)

    @pytest.mark.asyncio
    async def test_product_mismatch_rejected(self, monkeypatch, parsed_transaction):
        svc, _ = _build_subscription_service(monkeypatch)

        # Receipt verifies for MONTHLY but the client claimed YEARLY.
        svc.receipt_validator.validate_apple_receipt = AsyncMock(
            return_value={"valid": True, "data": parsed_transaction, "error": None}
        )

        with pytest.raises(ValueError) as exc:
            await svc.verify_and_activate_purchase(
                user_id="u1",
                platform="ios",
                receipt_data="legacy",
                product_id="com.themirrorcollective.mirror.core.yearly",
                transaction_id="ot1",
            )
        assert "Product mismatch" in str(exc.value)

    @pytest.mark.asyncio
    async def test_concurrent_race_resolves_idempotently(
        self, monkeypatch, parsed_transaction
    ):
        """When two concurrent /verify-purchase calls reach the conditional
        put with the same original_transaction_id, only one wins. The
        loser must read back the winner's row and return it
        idempotently — no second SUBSCRIPTION_PURCHASED event."""
        svc, dynamodb = _build_subscription_service(monkeypatch)

        svc.receipt_validator.validate_apple_receipt = AsyncMock(
            return_value={"valid": True, "data": parsed_transaction, "error": None}
        )
        svc._update_user_subscription_status = AsyncMock(return_value=None)
        svc._log_subscription_event = AsyncMock(return_value=None)

        # Initial get_item returns None (we thought we were the first
        # request), but the conditional put loses to a concurrent
        # winner. Then get_item is called again to fetch the winner.
        winner = {
            "user_id": "u1",
            "subscription_id": "ot1",
            "product_id": "com.themirrorcollective.mirror.core.monthly",
            "status": "ACTIVE",
        }
        dynamodb.get_item = AsyncMock(side_effect=[None, winner])
        dynamodb.put_item_if_not_exists = AsyncMock(return_value=False)

        result = await svc.verify_and_activate_purchase(
            user_id="u1",
            platform="ios",
            receipt_data="legacy",
            product_id="com.themirrorcollective.mirror.core.monthly",
            transaction_id="ot1",
        )

        assert result["success"] is True
        assert result["idempotent"] is True
        assert result["subscription"] == winner
        # We MUST NOT fire a duplicate SUBSCRIPTION_PURCHASED event when
        # we lost the conditional-put race.
        svc._log_subscription_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_idempotency_uses_verified_original_transaction_id(
        self, monkeypatch, parsed_transaction
    ):
        """Idempotency key must come from the verified receipt, never
        from the client's claim. If the frontend sends a renewal's
        transaction_id, the backend should still dedupe by the
        canonical original_transaction_id."""
        svc, dynamodb = _build_subscription_service(monkeypatch)

        # Frontend claims the renewal id "renewal-tx-99", but the
        # verified receipt yields original_transaction_id "ot1".
        parsed_transaction["transaction_id"] = "renewal-tx-99"
        parsed_transaction["original_transaction_id"] = "ot1"
        svc.receipt_validator.validate_apple_receipt = AsyncMock(
            return_value={"valid": True, "data": parsed_transaction, "error": None}
        )

        # Simulate an existing subscription row keyed on the ORIGINAL id.
        existing = {
            "user_id": "u1",
            "subscription_id": "ot1",
            "product_id": "com.themirrorcollective.mirror.core.monthly",
            "status": "ACTIVE",
        }

        # The service should call get_item with subscription_id="ot1"
        # (from the verified receipt) — NOT "renewal-tx-99" (from the
        # client).
        async def assert_correct_lookup(_table, key):
            assert key == {"user_id": "u1", "subscription_id": "ot1"}
            return existing

        dynamodb.get_item = AsyncMock(side_effect=assert_correct_lookup)

        result = await svc.verify_and_activate_purchase(
            user_id="u1",
            platform="ios",
            receipt_data="legacy",
            product_id="com.themirrorcollective.mirror.core.monthly",
            transaction_id="renewal-tx-99",  # client's renewal id
        )

        assert result["idempotent"] is True
        assert result["subscription"] == existing
        dynamodb.put_item.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_trial_period_sets_status_trial(
        self, monkeypatch, parsed_transaction
    ):
        svc, dynamodb = _build_subscription_service(monkeypatch)

        parsed_transaction["is_trial_period"] = True
        svc.receipt_validator.validate_apple_receipt = AsyncMock(
            return_value={"valid": True, "data": parsed_transaction, "error": None}
        )
        svc._update_user_subscription_status = AsyncMock(return_value=None)
        svc._log_subscription_event = AsyncMock(return_value=None)

        result = await svc.verify_and_activate_purchase(
            user_id="u1",
            platform="ios",
            receipt_data="legacy",
            product_id="com.themirrorcollective.mirror.core.monthly",
            transaction_id="ot1",
        )

        assert result["success"] is True
        # subscription dict status must be "trial" — to_dict doesn't
        # surface is_in_trial directly, so status is the proxy.
        assert result["subscription"]["status"] == "trial"


# --------------------------------------------------------------------------- #
# restore_user_purchases — SKU whitelist defence
# --------------------------------------------------------------------------- #


class TestRestorePurchasesSkuWhitelist:
    @pytest.mark.asyncio
    async def test_restore_rejects_unknown_sku(self, monkeypatch):
        svc, dynamodb = _build_subscription_service(monkeypatch)

        # Forged receipt: signature verifies (mocked) but claims a SKU
        # that's not in our products catalog.
        svc.receipt_validator.validate_apple_receipt = AsyncMock(
            return_value={
                "valid": True,
                "data": {
                    "transaction_id": "t1",
                    "original_transaction_id": "ot1",
                    "product_id": "com.attacker.fakeproduct",
                    "purchase_date": "2026-01-01T00:00:00Z",
                    "expiry_date": "2026-02-01T00:00:00Z",
                    "auto_renew_enabled": True,
                    "price": 0.99,
                },
                "error": None,
            }
        )

        result = await svc.restore_user_purchases(
            user_id="u1",
            platform="ios",
            receipts=["legacy-receipt"],
        )

        # Restore returns a dict with errors[] and no restored subscriptions.
        assert result["restored_count"] == 0
        assert any("Unknown product_id" in err for err in result.get("errors", []))
        # No write happened.
        dynamodb.put_item.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Google parse_google_purchase — Phase A contract
# --------------------------------------------------------------------------- #


class TestParseGooglePurchase:
    def test_maps_order_id_to_transaction_fields(self):
        """parse_google_purchase must populate the contract fields that
        verify_and_activate_purchase expects, not just the legacy
        order_id key. Bug regression — pre-fix, Android purchases
        couldn't activate because the parser returned no
        transaction_id / original_transaction_id."""
        from src.app.services.receipt_validator import ReceiptValidator

        v = ReceiptValidator()
        parsed = v.parse_google_purchase(
            {
                "orderId": "GPA.1234-5678-9012-12345",
                "productId": "com.themirrorcollective.mirror.core.monthly",
                "startTimeMillis": "1700000000000",
                "expiryTimeMillis": "1730000000000",
                "autoRenewing": True,
                "paymentState": 1,
            }
        )

        assert parsed["transaction_id"] == "GPA.1234-5678-9012-12345"
        assert parsed["original_transaction_id"] == "GPA.1234-5678-9012-12345"
        assert parsed["product_id"] == "com.themirrorcollective.mirror.core.monthly"
        assert parsed["auto_renew_enabled"] is True
        assert parsed["is_trial_period"] is False
        assert parsed["purchase_date"].endswith("Z")
        assert parsed["expiry_date"].endswith("Z")

    def test_payment_state_2_signals_trial(self):
        from src.app.services.receipt_validator import ReceiptValidator

        v = ReceiptValidator()
        parsed = v.parse_google_purchase(
            {
                "orderId": "GPA.x",
                "productId": "com.themirrorcollective.mirror.core.monthly",
                "startTimeMillis": "1700000000000",
                "expiryTimeMillis": "1730000000000",
                "paymentState": 2,
            }
        )
        assert parsed["is_trial_period"] is True


# --------------------------------------------------------------------------- #
# Production startup safety guards
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# H1 — _update_user_subscription_status derives status from subscription
# --------------------------------------------------------------------------- #


class TestUserProfileStatusDerivation:
    """The profile's subscription_status must mirror the Subscription
    row's status, not be hardcoded "active". Trial users in particular
    must have profile.subscription_status="trial" so the entitlement
    matrix + frontend useEntitlement see consistent state."""

    @pytest.mark.asyncio
    async def test_trial_activation_writes_trial_status_to_profile(self, monkeypatch):
        from src.app.models.subscription import (
            BillingPeriod,
            Platform,
            Subscription,
            SubscriptionStatus,
            SubscriptionType,
        )
        from src.app.models.user_profile import UserProfile, UserStatus

        svc, dynamodb = _build_subscription_service(monkeypatch)

        # Existing profile in "none" status — pre-trial signup.
        profile = UserProfile(
            user_id="u1",
            email="u1@example.com",
            subscription_status="none",
            subscription_tier="free",
            status=UserStatus.CONFIRMED,
        )
        dynamodb.get_user_profile = AsyncMock(return_value=profile)
        dynamodb.update_user_profile = AsyncMock(return_value=profile)

        trial_subscription = Subscription(
            user_id="u1",
            subscription_id="ot1",
            product_id="com.themirrorcollective.mirror.core.monthly",
            subscription_type=SubscriptionType.MIRROR_BASIC,
            platform=Platform.IOS,
            status=SubscriptionStatus.TRIAL,
            billing_period=BillingPeriod.MONTHLY,
            price_usd=0.0,
            is_in_trial=True,
        )

        await svc._update_user_subscription_status("u1", trial_subscription)

        dynamodb.update_user_profile.assert_awaited_once()
        await_args = dynamodb.update_user_profile.await_args
        assert await_args is not None
        updated = await_args.args[0]
        assert updated.subscription_status == "trial"
        assert updated.subscription_tier == "basic"

    @pytest.mark.asyncio
    async def test_grace_period_writes_grace_period_status(self, monkeypatch):
        from src.app.models.subscription import (
            BillingPeriod,
            Platform,
            Subscription,
            SubscriptionStatus,
            SubscriptionType,
        )
        from src.app.models.user_profile import UserProfile, UserStatus

        svc, dynamodb = _build_subscription_service(monkeypatch)
        profile = UserProfile(
            user_id="u1",
            email="u1@example.com",
            subscription_status="active",
            subscription_tier="basic",
            status=UserStatus.CONFIRMED,
        )
        dynamodb.get_user_profile = AsyncMock(return_value=profile)
        dynamodb.update_user_profile = AsyncMock(return_value=profile)

        sub = Subscription(
            user_id="u1",
            subscription_id="ot1",
            product_id="com.themirrorcollective.mirror.core.monthly",
            subscription_type=SubscriptionType.MIRROR_BASIC,
            platform=Platform.IOS,
            status=SubscriptionStatus.GRACE_PERIOD,
            billing_period=BillingPeriod.MONTHLY,
            price_usd=15.99,
        )

        await svc._update_user_subscription_status("u1", sub)
        await_args = dynamodb.update_user_profile.await_args
        assert await_args is not None
        updated = await_args.args[0]
        assert updated.subscription_status == "grace_period"


# --------------------------------------------------------------------------- #
# H2 — webhook lifecycle handlers re-raise on failure
# --------------------------------------------------------------------------- #


class TestWebhookHandlersReRaise:
    """A DynamoDB / downstream failure inside a lifecycle handler must
    propagate so handle_apple_webhook returns 500 and Apple/Google
    retries. Swallowing meant the platform saw 200 OK and gave up,
    losing real-money state silently."""

    @pytest.mark.asyncio
    async def test_renewal_handler_failure_propagates_500(self, monkeypatch):
        svc, _ = _build_subscription_service(monkeypatch)

        from src.app.services import apple_app_store_client

        signed_tx = {
            "transactionId": "t2",
            "originalTransactionId": "ot1",
            "productId": "com.themirrorcollective.mirror.core.monthly",
        }
        monkeypatch.setattr(
            apple_app_store_client,
            "verify_signed_notification",
            lambda _: {
                "notificationType": "DID_RENEW",
                "data": {"signedTransactionInfo": "<inner JWS>"},
            },
        )
        monkeypatch.setattr(
            apple_app_store_client,
            "verify_signed_transaction",
            lambda _: signed_tx,
        )

        # Renewal handler explodes (e.g. DynamoDB throttled).
        svc._handle_subscription_renewal = AsyncMock(
            side_effect=RuntimeError("dynamodb throttled")
        )

        from src.app.core.exceptions import InternalServerError

        with pytest.raises(InternalServerError):
            await svc.handle_apple_webhook({"signedPayload": "<JWS>"})


# --------------------------------------------------------------------------- #
# H3 — _parse_product_id uses the products.py catalog
# --------------------------------------------------------------------------- #


class TestParseProductId:
    def test_resolves_known_skus(self, monkeypatch):
        from src.app.models.subscription import BillingPeriod, SubscriptionType

        svc, _ = _build_subscription_service(monkeypatch)

        sub_type, billing = svc._parse_product_id(
            "com.themirrorcollective.mirror.core.monthly"
        )
        assert sub_type == SubscriptionType.MIRROR_BASIC
        assert billing == BillingPeriod.MONTHLY

        sub_type, billing = svc._parse_product_id(
            "com.themirrorcollective.mirror.storage.yearly"
        )
        assert sub_type == SubscriptionType.STORAGE_ADD_ON
        assert billing == BillingPeriod.YEARLY

    def test_rejects_unknown_skus(self, monkeypatch):
        """Forged or typo'd SKUs must raise — no silent fallback to
        MIRROR_CORE / MONTHLY as the previous substring matcher did."""
        svc, _ = _build_subscription_service(monkeypatch)
        with pytest.raises(ValueError, match="Unknown product_id"):
            svc._parse_product_id("com.attacker.core.monthly.evil")


class TestProductionStartupGuards:
    def test_pubsub_verify_disabled_in_production_raises(self, monkeypatch):
        from src.app.handler import _enforce_production_safety_invariants

        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("GOOGLE_PUBSUB_VERIFY", "false")

        with pytest.raises(RuntimeError) as exc:
            _enforce_production_safety_invariants()
        assert "GOOGLE_PUBSUB_VERIFY" in str(exc.value)

    def test_pubsub_verify_disabled_outside_production_allowed(self, monkeypatch):
        from src.app.handler import _enforce_production_safety_invariants

        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.setenv("GOOGLE_PUBSUB_VERIFY", "false")

        # Must NOT raise — local/dev environments can intentionally bypass.
        _enforce_production_safety_invariants()

    def test_pubsub_verify_enabled_in_production_allowed(self, monkeypatch):
        from src.app.handler import _enforce_production_safety_invariants

        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("GOOGLE_PUBSUB_VERIFY", "true")

        _enforce_production_safety_invariants()
