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
        # The subscription row was written...
        assert dynamodb.put_item.await_count == 1
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
