"""
chat_media telemetry (Phase 2).

Chat media is never stored (D7), so these log lines are the ONLY operational
record that a photo turn happened. That makes them the sole source for:

* cost tracking (how many vision turns, at what size);
* latency against the 30s API Gateway ceiling;
* rejection rates, which tell us whether client-side validation is working;
* D4 health — how often the model actually returns an image_description.

What must NOT appear: the bytes, the base64, or the image description. The
description is a sentence about someone's private photo and is already
persisted on the chat message; duplicating it into CloudWatch would widen
exposure for no diagnostic gain.
"""

import json
import logging

from src.app.services.telemetry.chat_media_events import (
    EVENT_CHAT_MEDIA,
    emit_chat_media,
)


def captured_line(caplog) -> dict:
    """The single JSON line the emitter wrote."""
    records = [r for r in caplog.records if r.name == "telemetry.chat_media"]
    assert len(records) == 1, f"expected 1 telemetry line, got {len(records)}"
    return json.loads(records[0].getMessage())


class TestSuccessEvent:
    def test_emits_one_json_line_with_the_event_tag(self, caplog):
        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            emit_chat_media(
                user_id="user-123",
                kind="image",
                mime_type="image/jpeg",
                decoded_bytes=240_000,
                status="success",
                duration_ms=4120,
                model="gpt-4o-mini",
                description_returned=True,
            )

        line = captured_line(caplog)
        assert line["event"] == EVENT_CHAT_MEDIA
        assert line["status"] == "success"

    def test_records_the_fields_cost_and_latency_analysis_needs(self, caplog):
        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            emit_chat_media(
                user_id="user-123",
                kind="image",
                mime_type="image/jpeg",
                decoded_bytes=240_000,
                status="success",
                duration_ms=4120,
                model="gpt-4o-mini",
                description_returned=True,
            )

        line = captured_line(caplog)
        assert line["kind"] == "image"
        assert line["mime_type"] == "image/jpeg"
        assert line["decoded_bytes"] == 240_000
        assert line["duration_ms"] == 4120
        assert line["model"] == "gpt-4o-mini"

    def test_records_whether_a_description_came_back(self, caplog):
        # D4 health: if this trends to False, the JSON-mode prompt has drifted
        # and stored turns are degrading to a bare "[shared a photo]".
        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            emit_chat_media(
                user_id="u",
                kind="image",
                mime_type="image/jpeg",
                decoded_bytes=1,
                status="success",
                duration_ms=1,
                description_returned=False,
            )

        assert captured_line(caplog)["description_returned"] is False

    def test_includes_a_timestamp(self, caplog):
        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            emit_chat_media(
                user_id="u",
                kind="image",
                mime_type="image/jpeg",
                decoded_bytes=1,
                status="success",
                duration_ms=1,
            )

        assert captured_line(caplog)["ts"].endswith("Z")


class TestUserIdentityIsHashed:
    def test_the_raw_user_id_never_appears(self, caplog):
        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            emit_chat_media(
                user_id="cognito-sub-abc-123",
                kind="image",
                mime_type="image/jpeg",
                decoded_bytes=1,
                status="success",
                duration_ms=1,
            )

        rendered = json.dumps(captured_line(caplog))
        assert "cognito-sub-abc-123" not in rendered
        assert "user_hash" in captured_line(caplog)

    def test_the_hash_matches_the_other_telemetry_domains(self, caplog):
        # Same hash function as upload/reflection events so analyses can
        # correlate a user across domains without ever de-anonymising them.
        from src.app.services.telemetry.reflection_events import hash_user_id

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            emit_chat_media(
                user_id="user-123",
                kind="image",
                mime_type="image/jpeg",
                decoded_bytes=1,
                status="success",
                duration_ms=1,
            )

        assert captured_line(caplog)["user_hash"] == hash_user_id("user-123")


class TestRejectionAndDegradedEvents:
    def test_records_a_rejection_with_a_bounded_reason(self, caplog):
        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            emit_chat_media(
                user_id="u",
                kind="image",
                mime_type="image/heic",
                decoded_bytes=0,
                status="rejected",
                duration_ms=2,
                reject_reason="unsupported_mime",
            )

        line = captured_line(caplog)
        assert line["status"] == "rejected"
        assert line["reject_reason"] == "unsupported_mime"

    def test_records_a_degraded_turn_when_vision_failed(self, caplog):
        # The user still got a reply (template fallback), but the photo was
        # effectively ignored — worth distinguishing from success.
        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            emit_chat_media(
                user_id="u",
                kind="image",
                mime_type="image/jpeg",
                decoded_bytes=1000,
                status="degraded",
                duration_ms=21000,
            )

        assert captured_line(caplog)["status"] == "degraded"

    def test_truncates_an_overlong_reject_reason(self, caplog):
        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            emit_chat_media(
                user_id="u",
                kind="image",
                mime_type="image/jpeg",
                decoded_bytes=0,
                status="rejected",
                duration_ms=1,
                reject_reason="x" * 500,
            )

        assert len(captured_line(caplog)["reject_reason"]) <= 100


class TestNeverLeaksMediaContent:
    def test_rejects_being_handed_raw_bytes(self, caplog):
        # The signature takes a byte COUNT, not bytes. If a caller passes
        # bytes by mistake the emitter must not serialise them into the log.
        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            emit_chat_media(
                user_id="u",
                kind="image",
                mime_type="image/jpeg",
                decoded_bytes=b"\xff\xd8\xff rawbytes",  # type: ignore[arg-type]
                status="success",
                duration_ms=1,
            )

        rendered = json.dumps(captured_line(caplog))
        assert "rawbytes" not in rendered

    def test_has_no_field_for_the_image_description(self, caplog):
        # Deliberate: the description is a sentence about a private photo and
        # is already persisted on the chat message. Only the boolean ships.
        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            emit_chat_media(
                user_id="u",
                kind="image",
                mime_type="image/jpeg",
                decoded_bytes=1,
                status="success",
                duration_ms=1,
                description_returned=True,
            )

        line = captured_line(caplog)
        assert "image_description" not in line
        assert "description" not in json.dumps(line).replace("description_returned", "")


class TestNeverBreaksTheRequest:
    def test_swallows_a_serialisation_failure(self, caplog):
        # Telemetry must never turn a working chat turn into a 500.
        class Unserialisable:
            def __repr__(self):
                raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="telemetry.chat_media"):
            emit_chat_media(
                user_id="u",
                kind=Unserialisable(),  # type: ignore[arg-type]
                mime_type="image/jpeg",
                decoded_bytes=1,
                status="success",
                duration_ms=1,
            )
        # No exception escaped — that is the assertion.

    def test_swallows_a_logger_failure(self, caplog):
        broken = logging.getLogger("telemetry.chat_media.broken")

        class BoomHandler(logging.Handler):
            def emit(self, record):
                raise RuntimeError("sink down")

        broken.addHandler(BoomHandler())
        try:
            emit_chat_media(
                user_id="u",
                kind="image",
                mime_type="image/jpeg",
                decoded_bytes=1,
                status="success",
                duration_ms=1,
                log=broken,
            )
        finally:
            broken.handlers.clear()
