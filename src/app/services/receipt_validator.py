"""
Receipt validation service for Apple and Google IAP.

Apple flow (modern):
    Uses the App Store Server API
    (https://developer.apple.com/documentation/appstoreserverapi). The legacy
    verifyReceipt endpoint (https://buy.itunes.apple.com/verifyReceipt) was
    deprecated by Apple and is being turned off — see
    https://developer.apple.com/news/?id=koe9hryd. This module signs an
    ES256 JWT with the App Store Connect API key on each request, calls
    `/inApp/v1/transactions/{transactionId}`, and decodes the returned
    JWS-signed transaction payload.

    A legacy `verifyReceipt` fallback remains behind the
    ``LEGACY_APPLE_VERIFYRECEIPT_ENABLED`` env flag for emergency rollback
    only. It logs a deprecation warning on every call and is intended to be
    removed once the modern path is fully validated in production.

Google flow:
    Google's androidpublisher v3 client is reused at module scope (built
    once, cached) and the synchronous ``.execute()`` call is run inside
    ``asyncio.to_thread`` so it no longer blocks the event loop.

Performance notes:
    * The ``aiohttp.ClientSession`` used for Apple HTTP calls is created
      once at module level (lazy, double-checked-lock initialised) and
      reused across calls — avoiding a TLS handshake on every receipt
      validation.
    * The Google service builder is cached via ``functools.lru_cache``, so
      credentials loading, discovery, and HTTPS plumbing happen exactly
      once per process.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, is_dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp
import jwt

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Apple Root CA G3 — bundled as a project asset so JWS signature verification
# doesn't need a runtime network fetch. The PEM file is the publicly-published
# Apple Root CA G3 cert from https://www.apple.com/certificateauthority/
# AppleRootCA-G3.cer, converted from DER to PEM. SHA256 fingerprint:
#   63:34:3A:BF:B8:9A:6A:03:EB:B5:7E:9B:3F:5F:A7:BE:7C:4F:5C:75
#   6F:30:17:B3:A8:C4:88:C3:65:3E:91:79
# --------------------------------------------------------------------------- #
_APPLE_ROOT_CA_G3_PATH = (
    Path(__file__).resolve().parent.parent
    / "resources"
    / "apple_root_certificates"
    / "AppleRootCA-G3.pem"
)


# --------------------------------------------------------------------------- #
# Module-level singletons (shared aiohttp session + Google service)
# --------------------------------------------------------------------------- #

_session: Optional[aiohttp.ClientSession] = None
# Lock is allocated lazily by `_get_session_lock` because constructing
# ``asyncio.Lock()`` at module import time binds it to whatever event loop
# is current then — on Lambda that's the synthetic init loop, NOT the loop
# that serves the request. A Lock bound to a dead loop raises
# ``RuntimeError: Task got Future attached to a different loop`` the first
# time it is awaited.
_session_lock: Optional[asyncio.Lock] = None


def _get_session_lock() -> asyncio.Lock:
    global _session_lock
    if _session_lock is None:
        _session_lock = asyncio.Lock()
    return _session_lock


async def _get_session() -> aiohttp.ClientSession:
    """Return a process-wide ``aiohttp.ClientSession``.

    Constructing a session per request triggers a fresh TLS handshake every
    time, which on Lambda cold-paths adds 100-300ms. We use double-checked
    locking so concurrent first-callers don't race to create two sessions.
    """
    global _session
    if _session is not None and not _session.closed:
        return _session
    async with _get_session_lock():
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _session


async def close_session() -> None:
    """Close the shared session (used in tests / graceful shutdown).

    Also clears the lazy lock so a subsequent ``_get_session`` call in a
    different event loop (typical in pytest with fresh per-test loops)
    doesn't try to reuse a Lock bound to the previous loop.
    """
    global _session, _session_lock
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None
    _session_lock = None


@lru_cache(maxsize=1)
def _get_google_service() -> Optional[Any]:
    """Build and cache the Google Play Developer API client.

    Returns ``None`` if credentials are not configured — callers must
    check before use. Cached for the lifetime of the process so the
    discovery document, credential loading and HTTPS connection pool are
    set up exactly once.
    """
    key_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY")
    if not key_path:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        logger.error(
            "Google libraries not installed. Install: "
            "pip install google-auth google-api-python-client"
        )
        return None
    try:
        credentials = service_account.Credentials.from_service_account_file(
            key_path,
            scopes=["https://www.googleapis.com/auth/androidpublisher"],
        )
        return build(
            "androidpublisher",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )
    except Exception as e:  # noqa: BLE001 - bubble error to caller via None
        logger.error(f"Failed to build cached Google Play service: {e}")
        return None


def reset_google_service_cache() -> None:
    """Clear the cached Google service (used in tests)."""
    _get_google_service.cache_clear()


# --------------------------------------------------------------------------- #
# Apple App Store Server API helpers
# --------------------------------------------------------------------------- #

# App Store Server API base URLs — single endpoint per env; environment is
# carried in the signed JWS payload returned by Apple.
_APPLE_API_PRODUCTION = "https://api.storekit.itunes.apple.com"
_APPLE_API_SANDBOX = "https://api.storekit-sandbox.itunes.apple.com"


def _apple_jwt_credentials() -> Optional[Dict[str, str]]:
    """Read App Store Connect API key material from env.

    Required env vars:
      * APPLE_APP_STORE_KEY_ID     — the 10-char Key ID from App Store Connect
      * APPLE_APP_STORE_ISSUER_ID  — the team's issuer ID (UUID-ish string)
      * APPLE_APP_STORE_BUNDLE_ID  — the app's bundle id (audience)
      * APPLE_APP_STORE_PRIVATE_KEY or APPLE_APP_STORE_PRIVATE_KEY_PATH —
        PEM-encoded ES256 private key (or path to one)
    """
    key_id = os.getenv("APPLE_APP_STORE_KEY_ID")
    issuer_id = os.getenv("APPLE_APP_STORE_ISSUER_ID")
    bundle_id = os.getenv("APPLE_APP_STORE_BUNDLE_ID")
    private_key = os.getenv("APPLE_APP_STORE_PRIVATE_KEY")
    key_path = os.getenv("APPLE_APP_STORE_PRIVATE_KEY_PATH")

    if not (key_id and issuer_id and bundle_id):
        return None

    if not private_key and key_path:
        try:
            with open(key_path, "r", encoding="utf-8") as fh:
                private_key = fh.read()
        except OSError as e:
            logger.error(f"Failed to read APPLE_APP_STORE_PRIVATE_KEY_PATH: {e}")
            return None

    if not private_key:
        return None

    return {
        "key_id": key_id,
        "issuer_id": issuer_id,
        "bundle_id": bundle_id,
        "private_key": private_key,
    }


def _build_apple_jwt(creds: Dict[str, str]) -> str:
    """Sign an ES256 bearer JWT for the App Store Server API.

    Apple requires the token to be signed with the ECDSA P-256 key issued
    in App Store Connect, with ``aud=appstoreconnect-v1`` and a max
    lifetime of one hour (we use five minutes to keep the blast radius
    small if the token leaks). See:
    https://developer.apple.com/documentation/appstoreserverapi/generating_tokens_for_api_requests
    """
    now = int(time.time())
    payload = {
        "iss": creds["issuer_id"],
        "iat": now,
        "exp": now + 60 * 5,
        "aud": "appstoreconnect-v1",
        "bid": creds["bundle_id"],
        "nonce": uuid.uuid4().hex,
    }
    headers = {"kid": creds["key_id"], "typ": "JWT"}
    return jwt.encode(payload, creds["private_key"], algorithm="ES256", headers=headers)


def _decode_jws_payload(jws: str) -> Dict:
    """Decode a JWS payload WITHOUT signature verification.

    Used only for client-side ID extraction (`_extract_transaction_id`) —
    we read the `transactionId` field from a receipt-shaped string the
    mobile client sent, then call Apple's API to fetch the authoritative
    transaction record. A forged JWS here would yield a bogus
    transactionId, Apple's API would 404, and the validation rejects.
    Signature verification is therefore not required on this path.

    For verifying Apple's RESPONSE payloads (signedTransactionInfo from
    /inApp/v1/transactions/...), use ``_verify_apple_jws`` instead —
    that path IS security-critical and DOES verify the x5c chain back
    to Apple Root CA G3.
    """
    if not jws or not isinstance(jws, str):
        return {}
    try:
        return jwt.decode(jws, options={"verify_signature": False}) or {}
    except jwt.PyJWTError as e:
        logger.error(f"Failed to decode Apple JWS payload: {e}")
        return {}


# --------------------------------------------------------------------------- #
# Apple JWS signature verification (App Store Server Library)
# --------------------------------------------------------------------------- #


class JWSVerificationError(Exception):
    """Raised when Apple's signedTransactionInfo fails signature verification.

    Distinct from `AppleTransactionError` (network/HTTP failure) and from
    "transaction not found" (None). A `JWSVerificationError` is a security
    signal — the JWS was either tampered, signed with a non-Apple key, or
    has an invalid certificate chain. The caller must NEVER grant
    entitlements on this path.
    """


def _load_apple_root_ca_g3() -> bytes:
    """Load Apple Root CA G3 bytes from the bundled PEM asset.

    Cached implicitly via lru_cache on the calling factory.
    """
    if not _APPLE_ROOT_CA_G3_PATH.is_file():
        raise FileNotFoundError(
            f"Apple Root CA G3 PEM not found at {_APPLE_ROOT_CA_G3_PATH}. "
            "JWS verification cannot proceed."
        )
    return _APPLE_ROOT_CA_G3_PATH.read_bytes()


@lru_cache(maxsize=2)
def _get_apple_signed_data_verifier(*, sandbox: bool) -> Any:
    """Return a SignedDataVerifier for the production or sandbox environment.

    Cached at module scope so the x5c chain + root cert parsing happens
    exactly once per environment per Lambda container. The SDK is
    intentionally imported lazily — modules outside the Apple validation
    path shouldn't pay for the dep at cold start.
    """
    from appstoreserverlibrary.models.Environment import Environment
    from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

    bundle_id = os.getenv("APPLE_APP_STORE_BUNDLE_ID", "")
    if not bundle_id:
        raise RuntimeError(
            "APPLE_APP_STORE_BUNDLE_ID env var is required for JWS verification."
        )
    # app_apple_id is REQUIRED for production verification. We accept 0 for
    # sandbox (the SDK tolerates it there); production deploys MUST set
    # APPLE_APP_STORE_APP_APPLE_ID to the numeric App ID from App Store
    # Connect (Apple's internal identifier, not the bundle ID).
    app_apple_id = int(os.getenv("APPLE_APP_STORE_APP_APPLE_ID", "0") or "0")
    env = Environment.SANDBOX if sandbox else Environment.PRODUCTION

    return SignedDataVerifier(
        root_certificates=[_load_apple_root_ca_g3()],
        enable_online_checks=False,
        bundle_id=bundle_id,
        app_apple_id=app_apple_id,
        environment=env,
    )


def _reset_apple_verifier_cache() -> None:
    """Test-only: clear the verifier cache so env-var changes take effect."""
    _get_apple_signed_data_verifier.cache_clear()


def _verify_apple_jws(jws: str, *, sandbox: bool) -> Dict[str, Any]:
    """Verify a signedTransactionInfo JWS from Apple and decode the payload.

    Verification covers:
      - x5c certificate chain back to Apple Root CA G3
      - bundle_id matches APPLE_APP_STORE_BUNDLE_ID
      - app_apple_id matches APPLE_APP_STORE_APP_APPLE_ID (prod only)
      - environment claim matches the API endpoint we hit (prod vs sandbox)
      - JWT signature is valid for the leaf cert's public key

    Args:
        jws: The signedTransactionInfo string returned by Apple's
             /inApp/v1/transactions/{transactionId} endpoint.
        sandbox: True if the JWS came from the sandbox API endpoint.

    Returns:
        Dict shape matching the previous unverified decode (so the caller
        doesn't change): keys like transactionId, productId, expiresDate,
        isTrialPeriod, autoRenewStatus, etc.

    Raises:
        JWSVerificationError: if the signature, chain, bundle_id, or
            app_apple_id check fails. Caller must treat this as a hard
            denial — never grant entitlements.
    """
    if not jws or not isinstance(jws, str):
        raise JWSVerificationError("Empty or non-string JWS")

    try:
        from appstoreserverlibrary.signed_data_verifier import VerificationException
    except ImportError as e:
        raise JWSVerificationError(
            "app-store-server-library not installed; JWS verification "
            "cannot proceed."
        ) from e

    verifier = _get_apple_signed_data_verifier(sandbox=sandbox)
    try:
        payload = verifier.verify_and_decode_signed_transaction(jws)
    except VerificationException as e:
        # SDK raises VerificationException for any failure: bad signature,
        # cert chain mismatch, bundle_id mismatch, app_id mismatch, expired
        # cert, etc. Treat all uniformly as "do not trust this payload."
        logger.warning(
            f"Apple JWS signature verification failed (sandbox={sandbox}): {e}"
        )
        raise JWSVerificationError(f"Apple JWS verification failed: {e}") from e
    except Exception as e:  # noqa: BLE001
        # Unexpected error during verification — defensively treat as a
        # security failure rather than a soft error.
        logger.error(
            f"Unexpected error during Apple JWS verification "
            f"(sandbox={sandbox}): {e}"
        )
        raise JWSVerificationError(f"JWS verification error: {e}") from e

    # SDK returns a typed payload (JWSTransactionDecodedPayload, a dataclass).
    # Convert to dict for caller compatibility.
    return _payload_to_dict(payload)


def _payload_to_dict(payload: Any) -> Dict[str, Any]:
    """Convert the SDK's typed payload into the dict shape the caller expects.

    The SDK returns a dataclass with snake_case attributes. The caller
    (``_validate_apple_modern``) currently reads camelCase keys via
    ``.get("transactionId")`` etc. We map both shapes for safety.
    """
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return payload
    if is_dataclass(payload):
        data = asdict(payload)
    else:
        # Try generic attribute scrape as a last resort.
        data = {
            k: getattr(payload, k)
            for k in dir(payload)
            if not k.startswith("_") and not callable(getattr(payload, k))
        }
    # Map snake_case → camelCase aliases so the existing caller code
    # (which reads camelCase keys) keeps working.
    snake_to_camel = {
        "transaction_id": "transactionId",
        "original_transaction_id": "originalTransactionId",
        "product_id": "productId",
        "purchase_date": "purchaseDate",
        "original_purchase_date": "originalPurchaseDate",
        "expires_date": "expiresDate",
        "offer_type": "offerType",
        "in_app_ownership_type": "inAppOwnershipType",
        "is_trial_period": "isTrialPeriod",
        "auto_renew_status": "autoRenewStatus",
    }
    for snake, camel in snake_to_camel.items():
        if snake in data and camel not in data:
            value = data[snake]
            # SDK enum types serialize awkwardly; convert to .value when
            # available so the result is JSON-friendly.
            if hasattr(value, "value"):
                value = value.value
            data[camel] = value
    return data


def _extract_transaction_id(receipt_data: str) -> Optional[str]:
    """Best-effort extraction of a transactionId from a receipt-shaped string.

    The mobile clients have historically sent one of:
      1. A bare transactionId (already what the modern API wants).
      2. A base64-encoded legacy receipt (what verifyReceipt accepts).
      3. A JWS-signed transaction (what StoreKit 2 emits via ``Transaction``).
    """
    if not receipt_data:
        return None

    s = receipt_data.strip()

    # Case 1: bare numeric / short identifier — looks like a transactionId.
    if len(s) < 64 and not s.startswith("ey") and "." not in s:
        return s

    # Case 3: JWS — three base64url segments separated by '.'.
    if s.count(".") == 2 and s.startswith("ey"):
        payload = _decode_jws_payload(s)
        tx = payload.get("transactionId") or payload.get("originalTransactionId")
        if tx:
            return str(tx)

    # Case 2: legacy base64 receipt — opaque blob. We can't reliably extract
    # a transactionId without ASN.1 PKCS#7 parsing, so we return None and let
    # the caller fall through to the legacy verifyReceipt path (if enabled)
    # or surface a clear error.
    return None


class AppleTransactionError(Exception):
    """Raised when Apple's transactions API returns a non-recoverable error.

    Distinct from "not found" (which returns None) so the caller can
    distinguish "try sandbox" from "fatal — propagate to client."
    """


async def _apple_get_transaction(
    transaction_id: str, jwt_token: str, *, sandbox: bool
) -> Optional[Dict]:
    """GET /inApp/v1/transactions/{transactionId} on production or sandbox.

    Returns:
        - dict on 200 OK (the transaction payload)
        - None on 404 (transaction genuinely doesn't exist on this env)

    Raises:
        AppleTransactionError on 4xx (other than 404) or 5xx. We MUST NOT
        silently fall through to sandbox on auth failures (401), rate
        limits (429), or server errors (5xx) — sandbox 200 on a forged
        transaction would otherwise grant production entitlements.
    """
    base = _APPLE_API_SANDBOX if sandbox else _APPLE_API_PRODUCTION
    url = f"{base}/inApp/v1/transactions/{transaction_id}"
    headers = {"Authorization": f"Bearer {jwt_token}"}
    session = await _get_session()
    async with session.get(url, headers=headers) as resp:
        if resp.status == 404:
            return None
        if resp.status >= 400:
            body = await resp.text()
            env = "sandbox" if sandbox else "production"
            logger.error(
                f"Apple {env} transactions GET failed: "
                f"status={resp.status} body={body[:300]}"
            )
            raise AppleTransactionError(
                f"Apple {env} transactions API returned HTTP {resp.status}"
            )
        return await resp.json()


# --------------------------------------------------------------------------- #
# Public class — interface stable for subscription_service.py
# --------------------------------------------------------------------------- #


class ReceiptValidator:
    """Validate IAP receipts with Apple and Google servers."""

    # Legacy endpoints — retained for emergency rollback only.
    APPLE_PRODUCTION_URL = "https://buy.itunes.apple.com/verifyReceipt"
    APPLE_SANDBOX_URL = "https://sandbox.itunes.apple.com/verifyReceipt"
    GOOGLE_API_URL = "https://androidpublisher.googleapis.com/androidpublisher/v3"

    def __init__(self) -> None:
        self.apple_shared_secret = os.getenv("APPLE_SHARED_SECRET")
        self.google_service_account_key = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY")
        self.google_package_name = os.getenv(
            "GOOGLE_PACKAGE_NAME", "com.mirrorcollective.app"
        )

    # ----------------- Apple ----------------- #

    async def validate_apple_receipt(
        self, receipt_data: str, exclude_old_transactions: bool = True
    ) -> Dict:
        """Validate an Apple IAP receipt.

        Modern path: App Store Server API. Falls back to legacy
        verifyReceipt only when ``LEGACY_APPLE_VERIFYRECEIPT_ENABLED=true``
        and the modern credentials aren't configured (or we couldn't
        extract a transactionId from the supplied payload).

        Args:
            receipt_data: Either a transactionId, a StoreKit 2 JWS, or
                (legacy) a base64-encoded receipt.
            exclude_old_transactions: Legacy-only flag — ignored by the
                modern API. Retained for caller-interface stability.

        Returns:
            ``{"valid": bool, "data": dict, "error": Optional[str]}``
        """
        # Unused on the modern path; documented in the docstring.
        del exclude_old_transactions

        try:
            creds = _apple_jwt_credentials()
            if creds is None:
                return await self._validate_apple_legacy_or_error(receipt_data)

            transaction_id = _extract_transaction_id(receipt_data)
            if not transaction_id:
                return await self._validate_apple_legacy_or_error(receipt_data)

            return await self._validate_apple_modern(transaction_id, creds)
        except Exception as e:  # noqa: BLE001 - return canonical error envelope
            logger.error(f"Error validating Apple receipt: {e}")
            return {"valid": False, "data": None, "error": str(e)}

    async def _validate_apple_modern(
        self, transaction_id: str, creds: Dict[str, str]
    ) -> Dict:
        token = _build_apple_jwt(creds)

        # Try production first; on 404, fall through to sandbox (mirrors
        # the legacy 21007 sandbox-receipt-sent-to-prod behaviour).
        #
        # CRITICAL: only 404 triggers the sandbox fallthrough. Auth failures
        # (401), rate limits (429), and server errors (5xx) from production
        # MUST propagate as errors — silently falling through on those would
        # let a sandbox 200 on a forged transaction grant production
        # entitlements. _apple_get_transaction raises AppleTransactionError
        # on any 4xx/5xx other than 404; we let it propagate to the outer
        # except in validate_apple_receipt.
        body = await _apple_get_transaction(transaction_id, token, sandbox=False)
        is_sandbox = False
        if body is None:
            body = await _apple_get_transaction(transaction_id, token, sandbox=True)
            is_sandbox = True

        if body is None:
            return {
                "valid": False,
                "data": None,
                "error": "Apple transaction not found in production or sandbox",
            }

        signed_tx = body.get("signedTransactionInfo")
        if not signed_tx:
            return {
                "valid": False,
                "data": None,
                "error": "Apple returned no signed transaction payload",
            }

        # CRITICAL: verify the JWS signature against Apple Root CA G3 before
        # trusting any field in the payload. A JWSVerificationError here is
        # a security signal — never grant entitlements.
        try:
            decoded = _verify_apple_jws(signed_tx, sandbox=is_sandbox)
        except JWSVerificationError as e:
            logger.warning(
                f"Apple JWS verification failed for tx={transaction_id} "
                f"(sandbox={is_sandbox}): {e}"
            )
            return {
                "valid": False,
                "data": None,
                "error": f"Apple JWS signature verification failed: {e}",
            }

        return {
            "valid": True,
            "data": self.parse_apple_transaction(decoded),
            "error": None,
        }

    async def _validate_apple_legacy_or_error(self, receipt_data: str) -> Dict:
        """Fallback path — only used if the legacy escape hatch is enabled."""
        legacy_enabled = (
            os.getenv("LEGACY_APPLE_VERIFYRECEIPT_ENABLED", "").lower() == "true"
        )
        if not legacy_enabled:
            return {
                "valid": False,
                "data": None,
                "error": (
                    "Apple App Store Server API credentials are not configured "
                    "(APPLE_APP_STORE_KEY_ID / ISSUER_ID / BUNDLE_ID / "
                    "PRIVATE_KEY). Set LEGACY_APPLE_VERIFYRECEIPT_ENABLED=true "
                    "to allow the deprecated verifyReceipt fallback."
                ),
            }

        logger.warning(
            "Using deprecated Apple verifyReceipt endpoint — this is a "
            "temporary emergency-rollback path. Configure "
            "APPLE_APP_STORE_KEY_ID, APPLE_APP_STORE_ISSUER_ID, "
            "APPLE_APP_STORE_BUNDLE_ID and APPLE_APP_STORE_PRIVATE_KEY "
            "to switch to the supported App Store Server API."
        )
        return await self._validate_apple_legacy(receipt_data)

    async def _validate_apple_legacy(self, receipt_data: str) -> Dict:
        """Legacy verifyReceipt call — kept for emergency rollback only."""
        request_body = {
            "receipt-data": receipt_data,
            "password": self.apple_shared_secret,
            "exclude-old-transactions": True,
        }

        session = await _get_session()
        async with session.post(
            self.APPLE_PRODUCTION_URL, json=request_body
        ) as response:
            result = await response.json()
            if result.get("status") == 21007:
                async with session.post(
                    self.APPLE_SANDBOX_URL, json=request_body
                ) as sandbox_response:
                    result = await sandbox_response.json()

        if result.get("status") == 0:
            return {
                "valid": True,
                "data": self.parse_apple_receipt(result),
                "error": None,
            }

        error_codes = {
            21000: "Malformed request",
            21002: "Invalid receipt data",
            21003: "Receipt authentication failed",
            21005: "Server unavailable",
            21008: "Receipt from wrong environment",
        }
        error_msg = error_codes.get(
            result.get("status"), f"Unknown error: {result.get('status')}"
        )
        return {"valid": False, "data": None, "error": error_msg}

    # ----------------- Google ----------------- #

    async def validate_google_receipt(
        self, receipt_data: str, product_id: Optional[str] = None
    ) -> Dict:
        """Validate an Android subscription purchase via Google Play API.

        ``service.purchases()...execute()`` is synchronous and blocks the
        event loop, so we hop into a worker thread via ``asyncio.to_thread``.
        """
        try:
            package_name = os.getenv("GOOGLE_PACKAGE_NAME")
            service_account_key_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY")

            if not package_name:
                logger.error("Google Play package name not configured")
                return {
                    "valid": False,
                    "error": (
                        "Google Play validation not configured. Set "
                        "GOOGLE_PACKAGE_NAME environment variable."
                    ),
                    "data": None,
                }

            if not service_account_key_path:
                logger.error("Google service account key not configured")
                return {
                    "valid": False,
                    "error": (
                        "Google Play validation not configured. Set "
                        "GOOGLE_SERVICE_ACCOUNT_KEY environment variable."
                    ),
                    "data": None,
                }

            if not product_id:
                logger.error("Product ID required for Google validation")
                return {
                    "valid": False,
                    "error": "Product ID required",
                    "data": None,
                }

            service = _get_google_service()
            if service is None:
                return {
                    "valid": False,
                    "error": (
                        "Google Play validation requires google-auth and "
                        "google-api-python-client. "
                        "Install: pip install google-auth google-api-python-client"
                    ),
                    "data": None,
                }

            try:
                request = (
                    service.purchases()
                    .subscriptions()
                    .get(
                        packageName=package_name,
                        subscriptionId=product_id,
                        token=receipt_data,
                    )
                )
                # Hop off the event loop — googleapiclient is synchronous and
                # would otherwise block other concurrent requests.
                result = await asyncio.to_thread(request.execute)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Google Play API error: {e}")
                return {
                    "valid": False,
                    "error": f"Google Play API error: {str(e)}",
                    "data": None,
                }

            # 0=pending, 1=received, 2=free trial, 3=pending deferred change.
            payment_state = result.get("paymentState")
            if payment_state not in [1, 2]:
                return {
                    "valid": False,
                    "error": f"Invalid payment state: {payment_state}",
                    "data": None,
                }

            return {
                "valid": True,
                "data": self.parse_google_purchase(result),
                "error": None,
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error validating Google receipt: {e}")
            return {"valid": False, "error": str(e), "data": None}

    # ----------------- Parsing ----------------- #

    def parse_apple_transaction(self, tx: Dict) -> Dict:
        """Parse a modern App Store Server API JWSTransaction payload.

        Field reference:
        https://developer.apple.com/documentation/appstoreserverapi/jwstransactiondecodedpayload
        """
        try:
            return {
                "transaction_id": tx.get("transactionId"),
                "original_transaction_id": tx.get("originalTransactionId"),
                "product_id": tx.get("productId"),
                "purchase_date_ms": tx.get("purchaseDate"),
                "expires_date_ms": tx.get("expiresDate"),
                "is_trial_period": tx.get("offerType") == 1,
                "is_in_intro_offer_period": tx.get("offerType") in (2, 3),
                "cancellation_date_ms": tx.get("revocationDate"),
                "auto_renew_status": tx.get("type") == "Auto-Renewable Subscription",
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error parsing Apple transaction: {e}")
            return {}

    def parse_apple_receipt(self, receipt_info: Dict) -> Dict:
        """Parse a legacy verifyReceipt response (still used by fallback)."""
        try:
            latest_receipt_info = receipt_info.get("latest_receipt_info", [])
            if not latest_receipt_info:
                return {}
            latest = latest_receipt_info[-1]
            return {
                "transaction_id": latest.get("transaction_id"),
                "original_transaction_id": latest.get("original_transaction_id"),
                "product_id": latest.get("product_id"),
                "purchase_date_ms": latest.get("purchase_date_ms"),
                "expires_date_ms": latest.get("expires_date_ms"),
                "is_trial_period": latest.get("is_trial_period") == "true",
                "is_in_intro_offer_period": (
                    latest.get("is_in_intro_offer_period") == "true"
                ),
                "cancellation_date_ms": latest.get("cancellation_date_ms"),
                "auto_renew_status": (
                    receipt_info.get("pending_renewal_info", [{}])[0].get(
                        "auto_renew_status"
                    )
                    == "1"
                ),
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error parsing Apple receipt: {e}")
            return {}

    def parse_google_purchase(self, purchase_info: Dict) -> Dict:
        """Extract relevant subscription data from Google purchase."""
        try:
            return {
                "order_id": purchase_info.get("orderId"),
                "product_id": purchase_info.get("productId"),
                "purchase_time_ms": purchase_info.get("startTimeMillis"),
                "expiry_time_ms": purchase_info.get("expiryTimeMillis"),
                "auto_renewing": purchase_info.get("autoRenewing", False),
                "payment_state": purchase_info.get("paymentState"),
                "cancel_reason": purchase_info.get("cancelReason"),
                "user_cancellation_time_ms": purchase_info.get(
                    "userCancellationTimeMillis"
                ),
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error parsing Google purchase: {e}")
            return {}


# --------------------------------------------------------------------------- #
# Hidden re-exports for tests
# --------------------------------------------------------------------------- #

__all__ = [
    "ReceiptValidator",
    "close_session",
    "reset_google_service_cache",
]


# Make json/base64 available for tests that monkeypatch — keep top-level
# imports tidy without sacrificing the original module surface.
_ = (json, base64)
