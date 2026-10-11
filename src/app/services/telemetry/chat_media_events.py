"""MirrorGPT chat-media telemetry.

One event per photo turn — success / rejected / degraded. Emitted to
CloudWatch Logs as a single JSON line, tagged ``chat_media`` so Logs Insights
filters cleanly:

    fields @timestamp, status, decoded_bytes, duration_ms, description_returned
    | filter @message like /"event":"chat_media"/
    | stats count() as turns,
            avg(duration_ms) as avg_ms,
            pct(duration_ms, 95) as p95_ms,
            avg(decoded_bytes) as avg_bytes
            by status, bin(1d)

Why this matters more than usual: chat media is never stored (see the client
repo's ``docs/CHAT_MEDIA_V1_PLAN.md`` D7), so these log lines are the **only**
operational record that a photo turn ever happened. Four things depend on
them:

1. **Cost** — vision turns per day, and the payload sizes driving them.
2. **Latency** — ``/chat`` runs against a 30s API Gateway ceiling and an
   image turn now carries its own upload inside that window. The p95 here is
   the early-warning signal for 504s.
3. **Rejection rate** — a climbing ``rejected`` count means client-side
   validation has drifted from the server's, and users are seeing errors.
4. **D4 health** — ``description_returned`` trending false means the
   JSON-mode prompt has drifted and stored turns are degrading to a bare
   ``[shared a photo]``, quietly costing the archetype engine its input.

Separate module from ``upload_events`` for the reason that file gives for
itself: different domain, different fields, and keeping them apart means
nobody reading either has to mentally filter out the other.

Privacy
-------
- ``user_id`` is hashed at the boundary (SHA-256 first 32 chars), the same
  function the upload and reflection emitters use, so analyses can correlate
  a user across domains without de-anonymising them.
- **No bytes, no base64, no data URLs** — only a byte count.
- **No image description.** It is a sentence about someone's private photo,
  and it is already persisted on the chat message; duplicating it here would
  widen exposure for no diagnostic gain. Only the boolean ships.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .reflection_events import hash_user_id

logger = logging.getLogger("telemetry.chat_media")

EVENT_CHAT_MEDIA = "chat_media"

# Rejection reasons are short machine-readable slugs ("unsupported_mime",
# "payload_too_large"). Bounded defensively in case a caller passes an
# exception string through.
MAX_REJECT_REASON_LEN = 100


def emit_chat_media(
    user_id: str,
    kind: str,
    mime_type: str,
    decoded_bytes: int,
    status: str,
    duration_ms: int,
    *,
    model: Optional[str] = None,
    description_returned: Optional[bool] = None,
    audio_duration_ms: Optional[int] = None,
    reject_reason: Optional[str] = None,
    log: Optional[logging.Logger] = None,
) -> None:
    """Emit one ``chat_media`` telemetry event.

    Args:
        user_id: Cognito sub. Hashed before it reaches the log.
        kind: ``'image'`` (``'audio'`` once Phase 4 lands).
        mime_type: Normalized, allowlisted MIME.
        decoded_bytes: Size of the DECODED media. A count, never the bytes.
        status: ``'success'`` | ``'rejected'`` | ``'degraded'``.
            ``degraded`` means the user got a reply but the photo was
            effectively ignored (vision call failed, template fallback used)
            — worth distinguishing from success because the user paid the
            latency without getting the feature.
        duration_ms: Wall-clock for the media-handling portion of the turn.
        model: Vision model used, on success.
        description_returned: Whether the model returned an
            ``image_description`` (D4 health).
        audio_duration_ms: True audio length for a voice turn, which is the
            per-minute cost basis.
        reject_reason: Short slug, on rejection.
        log: Override sink, for tests.

    Never raises. A telemetry hiccup must not turn a working chat turn into
    a 500.
    """
    out_log = log or logger
    try:
        line: Dict[str, Any] = {
            "event": EVENT_CHAT_MEDIA,
            "user_hash": hash_user_id(user_id),
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            # Short bounded identifiers. Coerced to str and capped rather
            # than trusted, so a caller passing something unexpected (e.g.
            # raw bytes for a size) can't serialise media content into the
            # log line.
            "kind": _bounded(kind, 32),
            "mime_type": _bounded(mime_type, 64),
            "decoded_bytes": _as_int(decoded_bytes),
            "status": _bounded(status, 32),
            "duration_ms": _as_int(duration_ms),
        }

        if model is not None:
            line["model"] = _bounded(model, 64)
        if description_returned is not None:
            line["description_returned"] = bool(description_returned)
        if audio_duration_ms is not None:
            # True audio length, from the model's verbose_json response. The
            # only way to check whether the byte cap bounds duration the way
            # the plan assumed — we deliberately don't parse the container.
            # Transcription bills per minute, so this is also the real cost
            # basis for a voice turn.
            line["audio_duration_ms"] = _as_int(audio_duration_ms)
        if reject_reason is not None:
            line["reject_reason"] = _bounded(reject_reason, MAX_REJECT_REASON_LEN)

        out_log.info(json.dumps(line))
    except Exception as exc:  # pragma: no cover - defensive
        # Swallow: never let telemetry break the route. Logged so the gap is
        # visible rather than silent.
        try:
            out_log.warning(f"emit_chat_media swallowed exception: {exc}")
        except Exception:
            pass


def _bounded(value: Any, limit: int) -> str:
    """Coerce to a short string.

    Deliberately paranoid: this is the boundary that keeps media content out
    of the logs, so it never trusts that a caller passed a tidy identifier.
    """
    try:
        if isinstance(value, (bytes, bytearray)):
            # A caller confused bytes for a label. Record the shape, not the
            # content.
            return f"<{len(value)} bytes>"
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return "<unrenderable>"
    return text[:limit]


def _as_int(value: Any) -> int:
    """Coerce a size/duration to an int, defaulting to 0.

    Guards the same risk as ``_bounded``: if a caller passes raw bytes where
    a count belongs, we must not serialise them.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)  # float, numeric string
    except (TypeError, ValueError):
        return 0
