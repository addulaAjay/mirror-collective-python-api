"""Reflection Room telemetry emitter (spec §10).

V1 ships with a structured-log emitter; V2 can drop in a Mixpanel/Segment/
Kinesis emitter behind the same Protocol with no call-site changes.

Phase 6 wires the emitter into ``POST /practice/complete``. Phase 7 will add
the full event matrix from spec §10 (8 events) to the rest of the routes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Protocol, Union

logger = logging.getLogger("telemetry.reflection")

# Allowed event names per spec §10.
EVENT_ECHO_SIGNATURE_VIEW = "echo_signature_view"
EVENT_PRACTICE_EXPAND = "practice_expand"
EVENT_PRACTICE_COMPLETE = "practice_complete"
EVENT_PRACTICE_HELPFUL = "practice_helpful"
EVENT_PRACTICE_NOT_HELPFUL = "practice_not_helpful"
EVENT_NUDGE_OPENED = "nudge_opened"
EVENT_PRIVATE_MODE_REVEAL = "private_mode_reveal"
EVENT_ECHO_MAP_REFRESH = "echo_map_refresh"


class TelemetryEmitter(Protocol):
    """Interface for Reflection Room telemetry. V1 sends to structured logs."""

    def emit(self, event_name: str, *, user_hash: str, **fields: Any) -> None: ...


class StructuredLogEmitter:
    """Default V1 emitter — writes one JSON line per event to the logger.

    Includes a PII filter at the boundary: only int / float / bool / str values
    of ≤64 chars are forwarded. Anything richer (free-form text, embeddings) is
    dropped silently rather than logged.
    """

    MAX_STR_LEN = 64

    def __init__(self, logger_=None):
        self._log = logger_ or logger

    def emit(self, event_name: str, *, user_hash: str, **fields: Any) -> None:
        sanitized: Dict[str, Union[int, float, bool, str]] = {}
        for k, v in fields.items():
            if isinstance(v, bool) or isinstance(v, (int, float)):
                sanitized[k] = v
            elif isinstance(v, str) and len(v) <= self.MAX_STR_LEN:
                sanitized[k] = v
            # else: drop — keeps free-form text out of logs.
        payload = {
            "event": event_name,
            "user_hash": user_hash,
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **sanitized,
        }
        self._log.info(json.dumps(payload))


class _NoOpEmitter:
    """Used in tests that don't care about telemetry payload."""

    def emit(self, event_name: str, *, user_hash: str, **fields: Any) -> None:
        return None


def hash_user_id(user_id: str) -> str:
    """SHA-256 first 32 chars — same as ``PracticeCompletion.user_hash``."""
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]


# Module-level default emitter. Overridable via dependency injection in tests
# or via a setter at app boot if a different sink is preferred.
_default_emitter: TelemetryEmitter = StructuredLogEmitter()


def get_default_emitter() -> TelemetryEmitter:
    return _default_emitter


def set_default_emitter(emitter: TelemetryEmitter) -> None:
    """Replace the module-level emitter (V2 swap path or test injection)."""
    global _default_emitter
    _default_emitter = emitter
