"""
Google Cloud Pub/Sub push verification + decoding for Google Play RTDN
webhooks.

Two responsibilities, separated so the route layer can return clear
status codes:

  1. `verify_pubsub_jwt(auth_header)` — verifies the OIDC token Google
     attaches to each push delivery. Without this, anyone who knows the
     webhook URL could forge "subscription cancelled" events and revoke
     a user's entitlement. Returns True iff the JWT is present,
     signature-valid, and the audience + service-account claims match
     env-configured values.

  2. `decode_pubsub_message(message_data)` — base64-decodes the inner
     Pub/Sub message body. Decode failures are NOT signature failures
     (that's #1) — they're payload format errors and should be surfaced
     as 400, not 401.

See docs/IAP_STORE_SETUP.md §B3 for the env-var configuration:

    GOOGLE_PUBSUB_AUDIENCE              — push subscription audience
    GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL — push subscription service account
    GOOGLE_PUBSUB_VERIFY                — false to skip (dev-only;
                                          guarded at startup in prod
                                          via handler.py)
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def decode_pubsub_message(message_data: str) -> Optional[Dict[str, Any]]:
    """Base64-decode a Pub/Sub `message.data` payload to a dict.

    Returns None on malformed input. Does NOT verify the OIDC JWT —
    that's `verify_pubsub_jwt` and must be called separately by the
    route handler with the inbound Authorization header.
    """
    try:
        decoded = base64.b64decode(message_data)
        notification = json.loads(decoded)
        logger.info("Decoded Google Pub/Sub notification")
        return notification
    except Exception as exc:
        logger.error("Error decoding Google Pub/Sub message: %s", exc)
        return None


def verify_pubsub_jwt(auth_header: Optional[str]) -> bool:
    """Verify the OIDC JWT Google attaches to each Pub/Sub push delivery.

    Token claims required:

        iss   = https://accounts.google.com
        aud   = GOOGLE_PUBSUB_AUDIENCE
        email = GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL  (verified)

    Returns True iff all of the above match. Returns False (and logs)
    on any failure. Callers must treat False as a hard 401.

    Setting `GOOGLE_PUBSUB_VERIFY=false` bypasses the check — for local
    dev only. The production startup guard in
    `src/app/handler.py::_enforce_production_safety_invariants` raises
    a RuntimeError if this is ever set in a production deployment.
    """
    if (os.getenv("GOOGLE_PUBSUB_VERIFY", "true") or "").lower() in (
        "0",
        "false",
        "no",
    ):
        logger.warning(
            "GOOGLE_PUBSUB_VERIFY is disabled — Pub/Sub JWT not verified. "
            "Do NOT use this setting in production."
        )
        return True

    if not auth_header:
        logger.warning("Google webhook missing Authorization header")
        return False

    if not auth_header.lower().startswith("bearer "):
        logger.warning("Google webhook Authorization header missing 'Bearer '")
        return False

    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return False

    expected_audience = os.getenv("GOOGLE_PUBSUB_AUDIENCE")
    expected_email = os.getenv("GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL")
    if not expected_audience or not expected_email:
        logger.error(
            "Google Pub/Sub JWT verification misconfigured: "
            "GOOGLE_PUBSUB_AUDIENCE and "
            "GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL are required."
        )
        return False

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=expected_audience,
        )
    except Exception as exc:
        logger.warning("Google Pub/Sub JWT verification failed: %s", exc)
        return False

    if claims.get("email") != expected_email:
        logger.warning(
            "Google Pub/Sub JWT email mismatch: got %s, expected %s",
            claims.get("email"),
            expected_email,
        )
        return False

    if not claims.get("email_verified", False):
        logger.warning("Google Pub/Sub JWT email_verified=false")
        return False

    return True
