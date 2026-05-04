"""Unit tests for the telemetry emitter (spec §10).

Covers spec §B.4 acceptance #15: ``IDs only, not text``. The PII filter at
the boundary must drop free-form long strings and reject non-primitive types.
"""

from __future__ import annotations

import json
import logging
from io import StringIO

from src.app.services.telemetry.reflection_events import (
    StructuredLogEmitter,
    hash_user_id,
)


def _capture_logger() -> tuple[logging.Logger, StringIO]:
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test.telemetry.capture")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger, buf


class TestStructuredLogEmitter:
    def test_emits_json_with_event_user_hash_and_ts(self):
        logger, buf = _capture_logger()
        emitter = StructuredLogEmitter(logger_=logger)
        emitter.emit("practice_complete", user_hash="abc123", loop_id="pressure")

        line = buf.getvalue().strip()
        payload = json.loads(line)
        assert payload["event"] == "practice_complete"
        assert payload["user_hash"] == "abc123"
        assert payload["loop_id"] == "pressure"
        assert "ts" in payload

    def test_drops_long_strings(self):
        logger, buf = _capture_logger()
        emitter = StructuredLogEmitter(logger_=logger)
        # 65-char string exceeds MAX_STR_LEN = 64.
        long_str = "x" * 65
        emitter.emit("practice_expand", user_hash="abc", note=long_str)
        payload = json.loads(buf.getvalue().strip())
        assert "note" not in payload

    def test_drops_non_primitive_values(self):
        logger, buf = _capture_logger()
        emitter = StructuredLogEmitter(logger_=logger)
        emitter.emit(
            "practice_complete",
            user_hash="abc",
            valid_int=5,
            valid_str="ok",
            invalid_dict={"nested": True},
            invalid_list=[1, 2, 3],
        )
        payload = json.loads(buf.getvalue().strip())
        assert payload["valid_int"] == 5
        assert payload["valid_str"] == "ok"
        assert "invalid_dict" not in payload
        assert "invalid_list" not in payload

    def test_keeps_bool_int_float_str(self):
        logger, buf = _capture_logger()
        emitter = StructuredLogEmitter(logger_=logger)
        emitter.emit(
            "practice_complete",
            user_hash="abc",
            count=3,
            score=0.75,
            helpful=True,
            tag="pressure",
        )
        payload = json.loads(buf.getvalue().strip())
        assert payload["count"] == 3
        assert payload["score"] == 0.75
        assert payload["helpful"] is True
        assert payload["tag"] == "pressure"


class TestHashUserId:
    def test_returns_32_hex_chars(self):
        h = hash_user_id("user-abc-123")
        assert len(h) == 32
        # All chars hex.
        assert all(c in "0123456789abcdef" for c in h)

    def test_is_deterministic(self):
        assert hash_user_id("u1") == hash_user_id("u1")

    def test_different_users_produce_different_hashes(self):
        assert hash_user_id("u1") != hash_user_id("u2")
