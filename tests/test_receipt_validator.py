"""
Tests for ``src.app.services.receipt_validator``.

These cover:
  * Apple modern path (App Store Server API + JWS decode).
  * Apple production -> sandbox fallback.
  * Legacy ``verifyReceipt`` fallback gated behind the emergency env flag.
  * Shared ``aiohttp.ClientSession`` reuse across calls.
  * Google ``service.execute()`` running via ``asyncio.to_thread`` (verified
    by overlapping multiple concurrent validations and asserting elapsed
    wallclock is below the serial floor).
"""

import asyncio
import time
from typing import Any, Dict
from unittest.mock import MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src.app.services import receipt_validator as rv_module
from src.app.services.receipt_validator import (
    ReceiptValidator,
    _extract_transaction_id,
    close_session,
    reset_google_service_cache,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_ec_pem() -> str:
    """Generate a throwaway ES256 private key for JWT signing tests."""
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")


def _sign_jws(payload: Dict[str, Any]) -> str:
    """Sign a payload with HS256 and return a JWT string.

    The receipt validator decodes Apple's JWS *without* signature verification,
    so the algorithm we use here is irrelevant — only the encoded payload
    matters.
    """
    return jwt.encode(
        payload, "test-secret-key-at-least-32-bytes-long!!", algorithm="HS256"
    )


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Make sure module-level singletons don't leak across tests."""
    rv_module._session = None
    reset_google_service_cache()
    yield
    rv_module._session = None
    reset_google_service_cache()


@pytest.fixture
def apple_creds_env(monkeypatch):
    pem = _make_ec_pem()
    monkeypatch.setenv("APPLE_APP_STORE_KEY_ID", "ABC1234567")
    monkeypatch.setenv(
        "APPLE_APP_STORE_ISSUER_ID", "12345678-1234-1234-1234-123456789012"
    )
    monkeypatch.setenv("APPLE_APP_STORE_BUNDLE_ID", "com.mirrorcollective.app")
    monkeypatch.setenv("APPLE_APP_STORE_PRIVATE_KEY", pem)
    monkeypatch.delenv("LEGACY_APPLE_VERIFYRECEIPT_ENABLED", raising=False)


# --------------------------------------------------------------------------- #
# _extract_transaction_id
# --------------------------------------------------------------------------- #


class TestExtractTransactionId:
    def test_bare_transaction_id_passes_through(self):
        assert _extract_transaction_id("1000000123456789") == "1000000123456789"

    def test_jws_payload_yields_transaction_id(self):
        token = _sign_jws({"transactionId": "tx-987", "productId": "p"})
        assert _extract_transaction_id(token) == "tx-987"

    def test_legacy_base64_blob_returns_none(self):
        # 200-char blob, no dots, doesn't start with 'ey' → unknown / opaque.
        blob = "A" * 200
        assert _extract_transaction_id(blob) is None

    def test_empty_returns_none(self):
        assert _extract_transaction_id("") is None


# --------------------------------------------------------------------------- #
# Apple modern path
# --------------------------------------------------------------------------- #


@pytest.fixture
def _bypass_jws_verification(monkeypatch):
    """Make the JWS verifier transparently fall back to unverified decode.

    Real verification needs an Apple-signed JWS with a chain rooted at
    Apple Root CA G3 — impossible to produce in unit tests without
    Apple's private keys. The verification path itself is tested
    separately in TestAppleJWSVerification (with mocks of the SDK).
    Other Apple-flow tests just need the decoded payload to flow
    through ``_validate_apple_modern``.
    """

    def _bypass(jws, *, sandbox):
        return rv_module._decode_jws_payload(jws)

    monkeypatch.setattr(rv_module, "_verify_apple_jws", _bypass)


@pytest.mark.asyncio
class TestAppleModernPath:
    async def test_returns_parsed_transaction_on_success(
        self, apple_creds_env, _bypass_jws_verification, monkeypatch
    ):
        validator = ReceiptValidator()
        signed_tx = _sign_jws(
            {
                "transactionId": "tx-1",
                "originalTransactionId": "orig-1",
                "productId": "com.mc.monthly",
                "purchaseDate": 1700000000000,
                "expiresDate": 1702592000000,
                "offerType": 1,
                "type": "Auto-Renewable Subscription",
            }
        )

        async def fake_get(transaction_id, token, *, sandbox):
            assert transaction_id == "tx-1"
            assert sandbox is False
            return {"signedTransactionInfo": signed_tx}

        monkeypatch.setattr(rv_module, "_apple_get_transaction", fake_get)

        result = await validator.validate_apple_receipt("tx-1")

        assert result["valid"] is True
        assert result["error"] is None
        assert result["data"]["transaction_id"] == "tx-1"
        assert result["data"]["product_id"] == "com.mc.monthly"
        assert result["data"]["is_trial_period"] is True
        assert result["data"]["auto_renew_status"] is True

    async def test_falls_back_to_sandbox_when_production_404s(
        self, apple_creds_env, _bypass_jws_verification, monkeypatch
    ):
        validator = ReceiptValidator()
        signed_tx = _sign_jws({"transactionId": "tx-2", "productId": "com.mc.yearly"})

        calls = []

        async def fake_get(transaction_id, token, *, sandbox):
            calls.append(sandbox)
            if not sandbox:
                return None  # simulate production 404
            return {"signedTransactionInfo": signed_tx}

        monkeypatch.setattr(rv_module, "_apple_get_transaction", fake_get)

        result = await validator.validate_apple_receipt("tx-2")

        assert calls == [False, True]
        assert result["valid"] is True
        assert result["data"]["transaction_id"] == "tx-2"

    async def test_returns_error_when_not_found_in_either_env(
        self, apple_creds_env, monkeypatch
    ):
        validator = ReceiptValidator()

        async def fake_get(*args, **kwargs):
            return None

        monkeypatch.setattr(rv_module, "_apple_get_transaction", fake_get)

        result = await validator.validate_apple_receipt("tx-missing")
        assert result["valid"] is False
        assert "not found" in result["error"]

    async def test_production_5xx_does_not_fall_through_to_sandbox(
        self, apple_creds_env, monkeypatch
    ):
        """Regression: a 5xx (or 401/429) from production MUST NOT cause a
        silent sandbox fallthrough — that would let a sandbox 200 on a
        forged transaction grant production entitlements.
        """
        from src.app.services.receipt_validator import AppleTransactionError

        validator = ReceiptValidator()
        calls = []

        async def fake_get(transaction_id, token, *, sandbox):
            calls.append(sandbox)
            if not sandbox:
                # Production raises (e.g. 503 / 401 / 429)
                raise AppleTransactionError(
                    "Apple production transactions API returned HTTP 503"
                )
            # If sandbox ever gets called, this would be the bug — return
            # a forged-looking valid transaction so the test can assert it
            # never reaches here.
            return {"signedTransactionInfo": "would-be-forged"}

        monkeypatch.setattr(rv_module, "_apple_get_transaction", fake_get)

        result = await validator.validate_apple_receipt("tx-3")

        # Sandbox path must NOT have been called.
        assert calls == [False], f"Production error fell through to sandbox: {calls}"
        # Caller sees a clear error, not a silent valid=True.
        assert result["valid"] is False
        assert "503" in str(result["error"]) or "production" in str(result["error"])


# --------------------------------------------------------------------------- #
# Apple JWS signature verification (App Store Server Library integration)
# --------------------------------------------------------------------------- #


class TestAppleJWSVerification:
    """Cover the new SignedDataVerifier-backed JWS verification path.

    Real verification needs Apple-signed JWS payloads (impossible to
    produce in unit tests without Apple's keys). We exercise our wrapper
    via the SDK's failure path (unsigned/foreign-signed JWS → SDK raises
    VerificationException → our wrapper raises JWSVerificationError).
    Integration with a real Apple-signed JWS belongs in an end-to-end
    test against Apple's sandbox API, not here.
    """

    @pytest.fixture(autouse=True)
    def _reset_verifier_cache(self, monkeypatch):
        """Clear the lru_cache so env-var changes per test take effect."""
        rv_module._reset_apple_verifier_cache()
        yield
        rv_module._reset_apple_verifier_cache()

    def test_empty_jws_is_rejected(self, apple_creds_env):
        """Empty/None/non-string JWS must raise — never silently pass."""
        from src.app.services.receipt_validator import (
            JWSVerificationError,
            _verify_apple_jws,
        )

        with pytest.raises(JWSVerificationError):
            _verify_apple_jws("", sandbox=False)
        with pytest.raises(JWSVerificationError):
            _verify_apple_jws(None, sandbox=False)  # type: ignore[arg-type]

    def test_jws_signed_with_non_apple_key_is_rejected(
        self, apple_creds_env, monkeypatch
    ):
        """A JWS signed with our test HS256 key (not Apple) must be rejected.

        The SDK's SignedDataVerifier walks the x5c chain back to Apple
        Root CA G3. An HS256 token has no x5c at all → VerificationException
        → JWSVerificationError.
        """
        from src.app.services.receipt_validator import (
            JWSVerificationError,
            _verify_apple_jws,
        )

        # An HS256-signed JWT that LOOKS like a transaction payload but
        # isn't signed by Apple.
        forged = _sign_jws(
            {
                "transactionId": "forged-tx-123",
                "productId": "com.attacker.premium",
            }
        )

        # APPLE_APP_STORE_APP_APPLE_ID is required for production
        # verification; set a non-zero placeholder so we're testing the
        # signature path, not a missing-config short-circuit.
        monkeypatch.setenv("APPLE_APP_STORE_APP_APPLE_ID", "1234567890")

        with pytest.raises(JWSVerificationError):
            _verify_apple_jws(forged, sandbox=False)

    def test_missing_bundle_id_env_raises(self, monkeypatch):
        """Without APPLE_APP_STORE_BUNDLE_ID, verifier construction must
        raise — never silently fall through."""
        from src.app.services.receipt_validator import _verify_apple_jws

        monkeypatch.delenv("APPLE_APP_STORE_BUNDLE_ID", raising=False)

        with pytest.raises(Exception) as exc_info:
            _verify_apple_jws("any-jws-content", sandbox=False)
        # Either the explicit RuntimeError from our factory or the
        # JWSVerificationError that wraps it — both acceptable signals.
        assert (
            "BUNDLE_ID" in str(exc_info.value)
            or "verification" in str(exc_info.value).lower()
        )

    @pytest.mark.asyncio
    async def test_validate_apple_modern_returns_error_on_verification_failure(
        self, apple_creds_env, monkeypatch
    ):
        """End-to-end: a JWS that fails verification produces
        valid=False with a clear error — never valid=True.

        This is the entitlement-fraud regression test: even if Apple's
        API responds 200 with a tampered payload, we reject.
        """
        validator = ReceiptValidator()

        forged_jws = _sign_jws(
            {
                "transactionId": "forged-tx-456",
                "productId": "com.attacker.premium",
            }
        )

        async def fake_get(transaction_id, token, *, sandbox):
            # Apple "returned 200" but the payload is forged.
            return {"signedTransactionInfo": forged_jws}

        monkeypatch.setattr(rv_module, "_apple_get_transaction", fake_get)
        # Real verifier in play (no _bypass_jws_verification fixture).
        monkeypatch.setenv("APPLE_APP_STORE_APP_APPLE_ID", "1234567890")
        rv_module._reset_apple_verifier_cache()

        result = await validator.validate_apple_receipt("tx-forged")

        assert (
            result["valid"] is False
        ), "Forged JWS produced valid=True — entitlement fraud risk!"
        assert (
            "verification failed" in (result["error"] or "").lower()
            or "verification" in (result["error"] or "").lower()
        )


# --------------------------------------------------------------------------- #
# Apple legacy fallback (emergency rollback path)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestAppleLegacyFallback:
    async def test_legacy_disabled_by_default_when_no_creds(self, monkeypatch):
        for var in (
            "APPLE_APP_STORE_KEY_ID",
            "APPLE_APP_STORE_ISSUER_ID",
            "APPLE_APP_STORE_BUNDLE_ID",
            "APPLE_APP_STORE_PRIVATE_KEY",
            "LEGACY_APPLE_VERIFYRECEIPT_ENABLED",
        ):
            monkeypatch.delenv(var, raising=False)

        validator = ReceiptValidator()
        result = await validator.validate_apple_receipt("legacy-blob")
        assert result["valid"] is False
        assert "App Store Server API credentials" in result["error"]

    async def test_legacy_path_hits_verifyReceipt_when_flag_enabled(self, monkeypatch):
        for var in (
            "APPLE_APP_STORE_KEY_ID",
            "APPLE_APP_STORE_ISSUER_ID",
            "APPLE_APP_STORE_BUNDLE_ID",
            "APPLE_APP_STORE_PRIVATE_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("LEGACY_APPLE_VERIFYRECEIPT_ENABLED", "true")
        monkeypatch.setenv("APPLE_SHARED_SECRET", "shared")

        validator = ReceiptValidator()

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self):
                return self._payload

        class FakeSession:
            closed = False

            def __init__(self):
                self.calls = []

            def post(self, url, json=None):
                self.calls.append(url)
                return FakeResp(
                    {
                        "status": 0,
                        "latest_receipt_info": [
                            {
                                "transaction_id": "L1",
                                "product_id": "p",
                                "purchase_date_ms": "1700000000000",
                                "expires_date_ms": "1702592000000",
                                "is_trial_period": "false",
                                "is_in_intro_offer_period": "false",
                            }
                        ],
                        "pending_renewal_info": [{"auto_renew_status": "1"}],
                    }
                )

        fake = FakeSession()

        async def fake_get_session():
            return fake

        monkeypatch.setattr(rv_module, "_get_session", fake_get_session)

        result = await validator.validate_apple_receipt("legacy-blob")
        assert result["valid"] is True
        assert result["data"]["transaction_id"] == "L1"
        assert any("verifyReceipt" in u for u in fake.calls)


# --------------------------------------------------------------------------- #
# Shared aiohttp session reuse
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestSharedAiohttpSession:
    async def test_session_is_reused_across_calls(self):
        s1 = await rv_module._get_session()
        s2 = await rv_module._get_session()
        try:
            assert s1 is s2
            assert not s1.closed
        finally:
            await close_session()

    async def test_session_recreated_after_close(self):
        s1 = await rv_module._get_session()
        await close_session()
        s2 = await rv_module._get_session()
        try:
            assert s1 is not s2
        finally:
            await close_session()


# --------------------------------------------------------------------------- #
# Google validation — caching + asyncio.to_thread
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestGoogleValidation:
    """Patches the module-level ``_get_google_service`` factory rather than
    ``googleapiclient.discovery.build`` so the tests run even in dev envs
    where ``google-api-python-client`` isn't installed.
    """

    async def test_service_is_built_once_and_reused(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GOOGLE_PACKAGE_NAME", "com.mc.app")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY", str(tmp_path / "sa.json"))

        build_calls = {"count": 0}

        fake_service = MagicMock()
        fake_service.purchases().subscriptions().get().execute.return_value = {
            "paymentState": 1,
            "orderId": "o1",
            "productId": "p1",
        }

        def fake_factory():
            build_calls["count"] += 1
            return fake_service

        from functools import lru_cache as _lru

        cached = _lru(maxsize=1)(fake_factory)
        monkeypatch.setattr(rv_module, "_get_google_service", cached)

        validator = ReceiptValidator()
        r1 = await validator.validate_google_receipt("tok-1", product_id="p1")
        r2 = await validator.validate_google_receipt("tok-2", product_id="p1")

        assert r1["valid"] is True
        assert r2["valid"] is True
        assert (
            build_calls["count"] == 1
        ), "Google service should be built once and cached across calls"

    async def test_execute_runs_on_thread_pool_not_event_loop(
        self, monkeypatch, tmp_path
    ):
        """If ``.execute()`` blocks the event loop, three concurrent calls
        would be serialized. We verify they overlap by timing wallclock vs
        the serial floor.
        """
        monkeypatch.setenv("GOOGLE_PACKAGE_NAME", "com.mc.app")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY", str(tmp_path / "sa.json"))

        SLEEP = 0.15

        def make_request_executor():
            req = MagicMock()

            def slow_execute():
                time.sleep(SLEEP)
                return {"paymentState": 1, "orderId": "o", "productId": "p"}

            req.execute = slow_execute
            return req

        fake_service = MagicMock()
        fake_service.purchases().subscriptions().get.side_effect = (
            lambda **kw: make_request_executor()
        )

        monkeypatch.setattr(rv_module, "_get_google_service", lambda: fake_service)

        validator = ReceiptValidator()

        start = time.monotonic()
        await asyncio.gather(
            validator.validate_google_receipt("tok-a", product_id="p"),
            validator.validate_google_receipt("tok-b", product_id="p"),
            validator.validate_google_receipt("tok-c", product_id="p"),
        )
        elapsed = time.monotonic() - start

        # If executes were serialized on the event loop elapsed >= 3*SLEEP.
        assert elapsed < (3 * SLEEP * 0.85), (
            f"Google .execute() appears to block the event loop "
            f"(elapsed={elapsed:.2f}s, serial-floor={3 * SLEEP:.2f}s)"
        )

    async def test_returns_error_for_invalid_payment_state(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GOOGLE_PACKAGE_NAME", "com.mc.app")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY", str(tmp_path / "sa.json"))

        fake_service = MagicMock()
        fake_service.purchases().subscriptions().get().execute.return_value = {
            "paymentState": 0  # pending — invalid
        }
        monkeypatch.setattr(rv_module, "_get_google_service", lambda: fake_service)

        validator = ReceiptValidator()
        result = await validator.validate_google_receipt("tok", product_id="p")
        assert result["valid"] is False
        assert "Invalid payment state" in result["error"]

    async def test_missing_product_id_returns_error(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_PACKAGE_NAME", "com.mc.app")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_KEY", "/tmp/x.json")

        validator = ReceiptValidator()
        result = await validator.validate_google_receipt("tok", product_id=None)
        assert result["valid"] is False
        assert "Product ID required" in result["error"]
