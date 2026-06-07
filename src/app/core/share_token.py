"""
Signed share tokens for the public echo viewer.

Email recipients aren't authenticated, so the "open your echo" link carries a
short, signed JWT that authorizes read-only access to ONE echo for ONE
recipient. The token is long-lived (the email may be opened weeks later) but
never embeds a presigned S3 URL — the viewer/redirect endpoints mint fresh
presigned URLs on demand, so links never go stale and access stays revocable
(rotate SHARE_TOKEN_SECRET).
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

import jwt

logger = logging.getLogger(__name__)

_ALG = "HS256"
_SCOPE = "echo_share"
_DEFAULT_TTL_DAYS = int(os.getenv("SHARE_TOKEN_TTL_DAYS", "90"))


def _secret() -> str:
    secret = os.getenv("SHARE_TOKEN_SECRET") or os.getenv("JWT_SECRET")
    if not secret:
        logger.warning(
            "SHARE_TOKEN_SECRET not set — using an insecure dev default. "
            "Set SHARE_TOKEN_SECRET in every non-local environment."
        )
        secret = "dev-insecure-share-secret-change-me"
    return secret


def create_share_token(
    echo_id: str, recipient_id: str, ttl_days: int = _DEFAULT_TTL_DAYS
) -> str:
    """Mint a signed token authorizing read access to one echo for one recipient."""
    now = datetime.now(timezone.utc)
    payload = {
        "echo_id": echo_id,
        "recipient_id": recipient_id,
        "scope": _SCOPE,
        "iat": now,
        "exp": now + timedelta(days=ttl_days),
    }
    return jwt.encode(payload, _secret(), algorithm=_ALG)


def verify_share_token(token: str, echo_id: str) -> Optional[Dict[str, Any]]:
    """Validate a share token and confirm it's bound to ``echo_id``.

    Returns the decoded payload (incl. recipient_id) or None if the token is
    invalid, expired, wrong-scope, or for a different echo.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALG])
    except jwt.PyJWTError as e:
        logger.info(f"Share token rejected: {e}")
        return None
    if payload.get("scope") != _SCOPE:
        return None
    if payload.get("echo_id") != echo_id:
        return None
    if not payload.get("recipient_id"):
        return None
    return payload


def share_base_url() -> str:
    """Public base URL where the viewer is served (the API's own origin)."""
    base = (
        os.getenv("SHARE_BASE_URL")
        or os.getenv("API_BASE_URL")
        or os.getenv("APP_URL")
        or "https://mirrorcollective.com"
    )
    return base.rstrip("/")


def build_share_url(echo_id: str, token: str) -> str:
    """Full viewer URL for an echo + token (used as the email CTA)."""
    return f"{share_base_url()}/share/echo/{quote(echo_id)}?t={quote(token)}"


def build_share_attachment_url(
    echo_id: str, attachment_id: str, token: str, mode: str = "view"
) -> str:
    """Durable URL for a single attachment (302 -> a fresh presigned S3 URL).

    Used as the email's inline hero/thumb <img> source so it shows the
    recipient's ACTUAL attached image and never expires — each fetch re-signs,
    unlike a one-shot presigned URL that dies after 7 days.
    """
    return (
        f"{share_base_url()}/share/echo/{quote(echo_id)}"
        f"/attachment/{quote(attachment_id)}?t={quote(token)}&mode={mode}"
    )
