"""Echo-vault upload telemetry.

One event per upload outcome — success / failed / aborted. Emitted to
CloudWatch Logs as a single JSON line, tagged ``upload_telemetry`` so
Logs Insights filters cleanly:

    fields @timestamp, upload_path, status, duration_total_ms,
           backgrounded_during_upload
    | filter @message like /"event":"echo_upload"/
    | stats count() as total,
            sum(backgrounded_during_upload) as bg
            by bin(7d)

Separate from the Reflection-Room telemetry emitter because:

1. The PII filter on ``StructuredLogEmitter`` caps strings at 64 chars,
   which would truncate our ``error_message`` (cap 200) mid-stack-frame.
2. Upload events need path scrubbing on ``error_message`` that
   reflection events don't.
3. Keeping the two domains in separate modules means the next person
   reading either one doesn't have to mentally filter out the other.

Privacy
-------
- ``user_id`` is hashed at the boundary (SHA-256 first 32 chars) — same
  as the reflection emitter so analyses can correlate across domains.
- ``error_message`` is truncated to 200 chars and scrubbed of any
  filesystem paths (``/var/mobile/...`` → ``[path]``).
- No media URLs, no S3 keys, no file content ever cross this boundary.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .reflection_events import hash_user_id

logger = logging.getLogger("telemetry.upload")

EVENT_ECHO_UPLOAD = "echo_upload"

# Cap matches the client-side truncation in uploadTelemetry.ts. If the
# client somehow sends more, we belt-and-suspender it here too.
MAX_ERROR_MESSAGE_LEN = 200

# Scrub anything that looks like a filesystem path. Catches:
#   - absolute Unix paths (/var/mobile/..., /Users/..., /Library/...)
#   - file:// URIs (file:///var/...)
#   - Android content URIs (content://media/...)
# Replaced with the literal token "[path]" so the log stays readable
# but doesn't leak device-local layout (which can carry usernames).
_PATH_RE = re.compile(
    r"(?:file://)?(?:content://[^\s]+|/(?:var|Users|Library|System|private|tmp)/[^\s]+)",
    re.IGNORECASE,
)


def _scrub_error_message(raw: str) -> str:
    """Truncate + path-scrub an error_message for safe logging."""
    if not raw:
        return ""
    scrubbed = _PATH_RE.sub("[path]", raw)
    if len(scrubbed) > MAX_ERROR_MESSAGE_LEN:
        scrubbed = scrubbed[:MAX_ERROR_MESSAGE_LEN]
    return scrubbed


def emit_echo_upload(
    user_id: str,
    payload: Dict[str, Any],
    *,
    log: Optional[logging.Logger] = None,
) -> None:
    """Emit one ``echo_upload`` telemetry event.

    The route layer is responsible for Pydantic validation; this function
    sanitizes + structures the final log line. Failures here are
    swallowed (logged at WARNING) so a telemetry hiccup never produces
    a non-204 response to the client.
    """
    out_log = log or logger
    try:
        sanitized: Dict[str, Any] = {}
        for k, v in payload.items():
            if k == "error_message" and isinstance(v, str):
                sanitized[k] = _scrub_error_message(v)
            elif v is None:
                sanitized[k] = None
            elif isinstance(v, (bool, int, float)):
                sanitized[k] = v
            elif isinstance(v, str):
                # Other strings (content_type, upload_path, status,
                # failure_stage, app_state_at_completion, platform,
                # app_version) are bounded short identifiers. Cap at
                # 64 chars defensively.
                sanitized[k] = v[:64]
            # Drop anything richer (nested objects, lists) — schema
            # doesn't expect them.

        line = {
            "event": EVENT_ECHO_UPLOAD,
            "user_hash": hash_user_id(user_id),
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **sanitized,
        }
        out_log.info(json.dumps(line))
    except Exception as exc:
        # Defensive: never let telemetry serialization break the route.
        out_log.warning(f"emit_echo_upload swallowed exception: {exc}")
