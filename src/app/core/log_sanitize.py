"""Helpers to keep PII out of logs.

Use these when a log line would otherwise contain personal data (chiefly email
addresses). They preserve enough signal to debug — a leading character and the
domain — without writing the full address to CloudWatch.
"""

from typing import Any


def mask_email(email: Any) -> str:
    """Mask an email for logging.

    ``"john.doe@example.com"`` -> ``"j***@example.com"``. Keeps the first
    character of the local part and the full domain. Returns ``"<redacted>"``
    for anything that isn't a usable address.
    """
    if not isinstance(email, str) or "@" not in email:
        return "<redacted>"
    local, _, domain = email.partition("@")
    if not local or not domain:
        return "<redacted>"
    return f"{local[0]}***@{domain}"
