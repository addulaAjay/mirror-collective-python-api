"""
Apple App Store Server API + StoreKit 2 signed-payload verification.

Replaces the deprecated `verifyReceipt` endpoint and the previously
insecure `jwt.decode(..., verify_signature=False)` pattern.

Two responsibilities:

1. **Receipt / subscription status** — given the `original_transaction_id`
   captured from the client's StoreKit 2 purchase result, fetch the
   authoritative subscription status from the App Store Server API
   (JWT-authenticated). Returns the latest signed transaction so we
   can both trust it (signature verified) and forward it to the
   subscription service.

2. **Signed-payload verification** — for ASSN v2 webhooks and any
   inbound StoreKit 2 signed transactions, verify the JWS x5c
   certificate chain terminates at Apple's published root CA before
   we trust the payload's claims.

See docs/IAP_STORE_SETUP.md §A4 for the four env vars this depends on
(`APPLE_ISSUER_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY`, `APPLE_BUNDLE_ID`)
and §A3 for the ASSN v2 webhook URL configuration.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Resource path to the Apple Root CA - G3 (DER-encoded). Bundled in the
# repo because the cert is public, tiny, and changes very rarely. If
# rotation is ever needed, run `scripts/fetch_apple_certs.py`.
_APPLE_ROOT_CERT_PATH = (
    Path(__file__).resolve().parent.parent
    / "resources"
    / "apple_root_certificates"
    / "AppleRootCA-G3.cer"
)


class AppleClientConfigError(RuntimeError):
    """Raised when the Apple SDK cannot be initialised due to missing config."""


class AppleSignatureVerificationError(RuntimeError):
    """Raised when a signed payload fails JWS x5c chain verification."""


def _load_root_certificates() -> list[bytes]:
    if not _APPLE_ROOT_CERT_PATH.exists():
        raise AppleClientConfigError(
            f"Apple root CA cert not found at {_APPLE_ROOT_CERT_PATH}. "
            "Run scripts/fetch_apple_certs.py to populate."
        )
    return [_APPLE_ROOT_CERT_PATH.read_bytes()]


def _env_or_none(name: str) -> Optional[str]:
    val = os.getenv(name)
    return val.strip() if val and val.strip() else None


def _resolve_environment():
    """Map APPLE_USE_SANDBOX env var to Environment enum.

    Default: production. Setting `APPLE_USE_SANDBOX=true` flips to
    sandbox, which matches the App Store Connect sandbox tester flow.
    """
    from appstoreserverlibrary.models.Environment import Environment

    use_sandbox = (os.getenv("APPLE_USE_SANDBOX", "false") or "").lower() in (
        "1",
        "true",
        "yes",
    )
    return Environment.SANDBOX if use_sandbox else Environment.PRODUCTION


@lru_cache(maxsize=1)
def _get_signed_data_verifier():
    """Build a SignedDataVerifier configured for this app's bundle.

    Cached at module level (the verifier holds the parsed root certs).
    Raises AppleClientConfigError if APPLE_BUNDLE_ID is missing — we
    can't verify a payload whose bundle we don't know.
    """
    from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

    bundle_id = _env_or_none("APPLE_BUNDLE_ID")
    if not bundle_id:
        raise AppleClientConfigError("APPLE_BUNDLE_ID env var is required.")

    root_certs = _load_root_certificates()
    env = _resolve_environment()

    # enable_online_checks=True hits Apple's CRL/OCSP at verify-time.
    # Adds latency (~100ms per call) but catches revoked Apple certs
    # immediately. Worth it for receipt validation; turn off only if
    # there's a strong performance reason.
    return SignedDataVerifier(
        root_certificates=root_certs,
        enable_online_checks=True,
        environment=env,
        bundle_id=bundle_id,
    )


@lru_cache(maxsize=1)
def _get_api_client():
    """Build an AppStoreServerAPIClient signed with the .p8 key.

    Reads APPLE_ISSUER_ID, APPLE_KEY_ID, APPLE_PRIVATE_KEY (PEM contents
    of the .p8 file from App Store Connect), and APPLE_BUNDLE_ID.

    Raises AppleClientConfigError on any missing config.
    """
    from appstoreserverlibrary.api_client import AppStoreServerAPIClient

    issuer_id = _env_or_none("APPLE_ISSUER_ID")
    key_id = _env_or_none("APPLE_KEY_ID")
    bundle_id = _env_or_none("APPLE_BUNDLE_ID")
    private_key_pem = _env_or_none("APPLE_PRIVATE_KEY")

    missing = [
        name
        for name, val in [
            ("APPLE_ISSUER_ID", issuer_id),
            ("APPLE_KEY_ID", key_id),
            ("APPLE_BUNDLE_ID", bundle_id),
            ("APPLE_PRIVATE_KEY", private_key_pem),
        ]
        if not val
    ]
    if missing:
        raise AppleClientConfigError(
            "Missing required Apple env vars: " + ", ".join(missing)
        )

    # `missing` was empty, so every var is a non-empty str. Re-assert
    # for the type checker (mypy can't follow the list-comprehension
    # narrowing).
    assert issuer_id is not None
    assert key_id is not None
    assert bundle_id is not None
    assert private_key_pem is not None

    return AppStoreServerAPIClient(
        signing_key=private_key_pem.encode("utf-8"),
        key_id=key_id,
        issuer_id=issuer_id,
        bundle_id=bundle_id,
        environment=_resolve_environment(),
    )


def reset_clients_for_testing() -> None:
    """Clear cached singletons. Tests use this to swap env between cases."""
    _get_signed_data_verifier.cache_clear()
    _get_api_client.cache_clear()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def verify_signed_transaction(signed_transaction: str) -> Dict[str, Any]:
    """Verify a StoreKit 2 signed transaction (JWS) and return its decoded payload.

    `signed_transaction` is the `jwsRepresentation` returned by Apple
    inside `Transaction.value` on the client. The verifier checks:

      - JWS signature against the x5c leaf certificate
      - x5c chain terminates at Apple Root CA - G3
      - Online CRL/OCSP check (revocation)
      - The bundleId claim matches our configured APPLE_BUNDLE_ID

    Raises AppleSignatureVerificationError on any failure.

    Returns the decoded payload as a dict for downstream consumption
    (the SDK returns a JWSTransactionDecodedPayload dataclass; we
    convert via dataclasses.asdict for ergonomic Dict access).
    """
    from appstoreserverlibrary.signed_data_verifier import VerificationException

    try:
        verifier = _get_signed_data_verifier()
        payload = verifier.verify_and_decode_signed_transaction(signed_transaction)
    except VerificationException as exc:
        logger.warning("Apple signed transaction verification failed: %s", exc)
        raise AppleSignatureVerificationError(str(exc)) from exc

    return _payload_to_dict(payload)


def verify_signed_notification(signed_payload: str) -> Dict[str, Any]:
    """Verify an ASSN v2 webhook payload (signedPayload) and return its decoded body.

    Apple posts ASSN v2 as `{"signedPayload": "<JWS>"}`. We verify the
    JWS x5c chain and return the decoded `responseBodyV2DecodedPayload`
    as a dict.

    Raises AppleSignatureVerificationError on any failure.
    """
    from appstoreserverlibrary.signed_data_verifier import VerificationException

    try:
        verifier = _get_signed_data_verifier()
        payload = verifier.verify_and_decode_notification(signed_payload)
    except VerificationException as exc:
        logger.warning("Apple ASSN v2 verification failed: %s", exc)
        raise AppleSignatureVerificationError(str(exc)) from exc

    return _payload_to_dict(payload)


def get_subscription_statuses(transaction_id: str) -> Dict[str, Any]:
    """Fetch the latest signed transaction info for a subscription.

    Replaces the deprecated verifyReceipt flow: instead of asking Apple
    to validate a base64 receipt blob the client sent us, we use the
    `originalTransactionId` (captured client-side after a successful
    purchase) to ask the App Store Server API directly. Apple returns
    a list of signed transactions; we extract and verify the latest.

    Returns a dict with keys:
      - environment: "Sandbox" | "Production"
      - signed_transactions: list of decoded transaction payloads
      - latest_signed_transaction: the most recent one, or None
    """
    from appstoreserverlibrary.api_client import APIException

    api = _get_api_client()
    try:
        # `get_all_subscription_statuses` returns a status response with
        # all subscriptions for this original_transaction_id grouped by
        # subscription group.
        resp = api.get_all_subscription_statuses(transaction_id=transaction_id)
    except APIException as exc:
        logger.warning(
            "App Store Server API call failed for transaction %s: %s",
            transaction_id,
            exc,
        )
        raise

    # Flatten + verify every signedTransactionInfo in the response.
    decoded_transactions: list[Dict[str, Any]] = []
    for group in resp.data or []:
        for last_tx in group.lastTransactions or []:
            signed = last_tx.signedTransactionInfo
            if signed:
                decoded_transactions.append(verify_signed_transaction(signed))

    # Most-recent by purchaseDate (transactions sorted ascending in
    # most groups, but don't assume — pick by date explicitly).
    latest = (
        max(
            decoded_transactions,
            key=lambda t: t.get("purchaseDate") or 0,
        )
        if decoded_transactions
        else None
    )

    return {
        "environment": (
            resp.environment.value
            if getattr(resp, "environment", None) is not None
            else None
        ),
        "signed_transactions": decoded_transactions,
        "latest_signed_transaction": latest,
    }


# --------------------------------------------------------------------------- #
# Webhook-side helpers — swallow-and-log wrappers around the verifiers.
#
# Webhook handlers want a "verify, log on failure, return None" shape so
# they can treat None as a hard reject without try/except sprawl. The
# raising verifiers above are still the right primitive for callers that
# need to surface the specific failure code (e.g. `verify_and_activate`).
# --------------------------------------------------------------------------- #


def try_verify_signed_notification(signed_payload: str) -> Optional[Dict[str, Any]]:
    """Webhook variant: returns decoded dict on success, None on any
    verification or configuration failure. Failures are logged at
    `warning` (signature) or `error` (config) — callers must NOT
    process a None result as if it were a valid empty payload.
    """
    try:
        return verify_signed_notification(signed_payload)
    except AppleSignatureVerificationError as exc:
        logger.warning("Rejecting Apple webhook: invalid signature: %s", exc)
        return None
    except AppleClientConfigError as exc:
        logger.error("Apple verifier misconfigured: %s", exc)
        return None


def try_verify_signed_transaction(signed_transaction: str) -> Optional[Dict[str, Any]]:
    """Same swallow-and-log shape as `try_verify_signed_notification`
    but for individual signedTransactionInfo payloads inside an ASSN v2
    body."""
    try:
        return verify_signed_transaction(signed_transaction)
    except AppleSignatureVerificationError as exc:
        logger.warning("Rejecting Apple transaction: invalid signature: %s", exc)
        return None
    except AppleClientConfigError as exc:
        logger.error("Apple verifier misconfigured: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #


def _payload_to_dict(payload: Any) -> Dict[str, Any]:
    """Convert a SDK dataclass payload to a plain dict.

    The SDK exposes payloads as @dataclass instances. `dataclasses.asdict`
    drops enums and dates into raw values, which is exactly what we
    want for JSON-friendly downstream consumption.
    """
    import dataclasses

    if dataclasses.is_dataclass(payload):
        return dataclasses.asdict(payload)

    # Some SDK objects expose a `to_dict()` method; fall back to that.
    if hasattr(payload, "to_dict") and callable(payload.to_dict):
        return payload.to_dict()

    # Last resort: dict() over public attributes.
    return {k: v for k, v in vars(payload).items() if not k.startswith("_")}
