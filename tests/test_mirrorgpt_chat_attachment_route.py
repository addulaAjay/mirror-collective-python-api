"""
End-to-end: POST /api/mirrorgpt/chat carrying an inline photo.

This is the Phase 1 + 2 milestone from the chat-media plan — the whole
backend feature exercised through the HTTP layer, with no client work
needed. What it pins:

* a base64 image in the request body reaches the vision path;
* hostile payloads are rejected with 4xx, not 500;
* the persisted user turn is TEXT (D4) and contains no image bytes (D7);
* text-only chat is completely unaffected.
"""

import base64
import json
import os
from typing import Any, Dict
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

for key, value in {
    "COGNITO_USER_POOL_ID": "testpoolid123",
    "COGNITO_CLIENT_ID": "testclientid123",
    "OPENAI_API_KEY": "test-openai-key",
    "AWS_REGION": "us-east-1",
    "ENVIRONMENT": "test",
    "DISABLE_AUTH": "true",
}.items():
    os.environ[key] = value

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
JPEG_B64 = base64.b64encode(JPEG_BYTES).decode()
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

VISION_JSON = json.dumps(
    {"reply": "Still water holds the whole sky.", "image_description": "a lake at dusk"}
)


def build_client(vision_payload: str = VISION_JSON):
    """TestClient with the orchestrator's OpenAI + persistence legs mocked.

    Returns (client, captured) where `captured` collects the persisted
    messages so assertions can inspect exactly what hit DynamoDB.
    """
    from src.app.api.mirrorgpt_routes import (
        get_conversation_service,
        get_mirror_orchestrator,
    )
    from src.app.core.enhanced_auth import get_user_with_profile
    from src.app.handler import app
    from src.app.services.mirror_orchestrator import MirrorOrchestrator

    captured: Dict[str, Any] = {"messages": []}

    # Real orchestrator + real ResponseGenerator, mocked OpenAI underneath —
    # so the content-parts construction and JSON parsing are genuinely
    # exercised rather than stubbed over.
    orchestrator = MirrorOrchestrator(dynamodb_service=Mock(), openai_service=Mock())
    orchestrator.response_generator.openai_service = Mock()
    orchestrator.response_generator.openai_service.send_with_overrides_async = (
        AsyncMock(return_value=vision_payload)
    )
    orchestrator.response_generator.openai_service.send_async = AsyncMock(
        return_value="A text-only reply."
    )
    orchestrator._get_user_profile = AsyncMock(return_value=None)  # type: ignore[method-assign]
    orchestrator._get_recent_signals_from_messages = AsyncMock(return_value=[])  # type: ignore[method-assign]
    orchestrator._get_conversation_history = AsyncMock(return_value=[])  # type: ignore[method-assign]
    orchestrator._load_preflight_data = AsyncMock(return_value=None)  # type: ignore[method-assign]
    orchestrator._update_user_profile = AsyncMock(return_value=None)  # type: ignore[method-assign]
    orchestrator._create_mirror_moment = AsyncMock(return_value=None)  # type: ignore[method-assign]

    conversation_service = Mock()

    async def record_user_message(**kwargs):
        captured["messages"].append(kwargs)
        return Mock(message_id="m1")

    conversation_service.add_message_with_mirrorgpt_analysis = AsyncMock(
        side_effect=record_user_message
    )
    conversation_service.add_message = AsyncMock(side_effect=record_user_message)
    conversation_service.create_conversation = AsyncMock(
        return_value=Mock(conversation_id="conv-1")
    )
    conversation_service.get_conversation = AsyncMock(
        return_value=Mock(conversation_id="conv-1")
    )
    conversation_service.maybe_summarize_after_chat = AsyncMock(return_value=None)

    async def fake_user():
        return {"id": "test-user-123", "email": "t@example.com", "fullName": "Test"}

    app.dependency_overrides[get_mirror_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_conversation_service] = lambda: conversation_service
    app.dependency_overrides[get_user_with_profile] = fake_user

    return TestClient(app), captured


def teardown_function():
    from src.app.handler import app

    app.dependency_overrides.clear()


def post(client, **body):
    payload = {"message": "", "session_id": "sess-1", "conversation_id": "conv-1"}
    payload.update(body)
    return client.post(
        "/api/mirrorgpt/chat",
        json=payload,
        headers={"Authorization": "Bearer test-token"},
    )


def an_image(mime="image/jpeg", data=None):
    return {
        "kind": "image",
        "mime_type": mime,
        "data_base64": data if data is not None else JPEG_B64,
    }


class TestHappyPath:
    def test_an_image_turn_returns_the_vision_reply(self):
        client, _ = build_client()

        resp = post(client, message="what do you see?", attachments=[an_image()])

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["response"] == "Still water holds the whole sky."

    def test_an_image_with_no_caption_is_accepted(self):
        # The headline new case. Before this work, min_length=1 on `message`
        # made it a 422.
        client, _ = build_client()

        resp = post(client, message="", attachments=[an_image()])

        assert resp.status_code == 200, resp.text

    def test_a_png_is_accepted(self):
        client, _ = build_client()

        resp = post(
            client,
            message="look",
            attachments=[an_image("image/png", base64.b64encode(PNG_BYTES).decode())],
        )

        assert resp.status_code == 200, resp.text


class TestWhatGetsPersisted:
    def test_persists_the_caption_and_the_description_as_text(self):
        client, captured = build_client()

        post(client, message="what do you see?", attachments=[an_image()])

        user_turn = next(m for m in captured["messages"] if m["role"] == "user")
        assert "what do you see?" in user_turn["content"]
        assert "a lake at dusk" in user_turn["content"]

    def test_persists_no_image_bytes_anywhere(self):
        # The D7 guarantee, asserted against everything written.
        client, captured = build_client()

        post(client, message="look", attachments=[an_image()])

        for message in captured["messages"]:
            serialized = json.dumps(message, default=str)
            assert JPEG_B64[:32] not in serialized
            assert "data:image" not in serialized
            assert "base64" not in serialized

    def test_a_no_caption_turn_persists_non_empty_content(self):
        # Otherwise _validate_message_inputs (non-empty content) would reject
        # the write and the turn would vanish.
        client, captured = build_client()

        post(client, message="", attachments=[an_image()])

        user_turn = next(m for m in captured["messages"] if m["role"] == "user")
        assert user_turn["content"].strip()

    def test_persists_the_assistant_reply_unchanged(self):
        client, captured = build_client()

        post(client, message="look", attachments=[an_image()])

        assistant = next(m for m in captured["messages"] if m["role"] == "assistant")
        assert assistant["content"] == "Still water holds the whole sky."


class TestRejections:
    def test_rejects_a_disallowed_mime_with_400(self):
        client, _ = build_client()

        resp = post(client, message="x", attachments=[an_image("image/heic")])

        assert resp.status_code == 400, resp.text

    def test_rejects_malformed_base64_with_400(self):
        client, _ = build_client()

        resp = post(
            client, message="x", attachments=[an_image(data="!!!not base64!!!")]
        )

        assert resp.status_code == 400, resp.text

    def test_rejects_a_mime_magic_byte_mismatch_with_400(self):
        client, _ = build_client()

        resp = post(
            client,
            message="x",
            attachments=[an_image("image/jpeg", base64.b64encode(PNG_BYTES).decode())],
        )

        assert resp.status_code == 400, resp.text

    def test_rejects_two_attachments_with_422(self):
        client, _ = build_client()

        resp = post(client, message="x", attachments=[an_image(), an_image()])

        assert resp.status_code == 422, resp.text

    def test_rejects_an_empty_message_with_no_attachment_with_422(self):
        client, _ = build_client()

        resp = post(client, message="")

        assert resp.status_code == 422, resp.text

    def test_rejects_an_unknown_kind_with_422(self):
        client, _ = build_client()

        resp = post(
            client,
            message="x",
            attachments=[
                {"kind": "video", "mime_type": "video/mp4", "data_base64": JPEG_B64}
            ],
        )

        assert resp.status_code == 422, resp.text

    def test_a_rejection_response_never_echoes_the_payload(self):
        # Validation messages reach both the HTTP body and CloudWatch.
        client, _ = build_client()

        resp = post(client, message="x", attachments=[an_image("image/heic")])

        assert JPEG_B64[:32] not in resp.text


class TestTextOnlyChatIsUnaffected:
    def test_a_text_only_turn_still_uses_the_text_path(self):
        client, captured = build_client()

        resp = post(client, message="just words")

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["response"] == "A text-only reply."

    def test_a_text_only_turn_persists_the_message_verbatim(self):
        client, captured = build_client()

        post(client, message="just words")

        user_turn = next(m for m in captured["messages"] if m["role"] == "user")
        assert user_turn["content"] == "just words"
        assert "shared a photo" not in user_turn["content"]


class TestTelemetry:
    """chat_media is the only operational record of a photo turn (D7)."""

    def _lines(self, caplog):
        return [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == "telemetry.chat_media"
        ]

    def test_emits_a_success_event_for_an_image_turn(self, caplog):
        import logging

        client, _ = build_client()

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            post(client, message="look", attachments=[an_image()])

        lines = self._lines(caplog)
        assert len(lines) == 1
        assert lines[0]["status"] == "success"
        assert lines[0]["kind"] == "image"
        assert lines[0]["mime_type"] == "image/jpeg"
        assert lines[0]["decoded_bytes"] == len(JPEG_BYTES)
        assert lines[0]["description_returned"] is True

    def test_records_a_duration(self, caplog):
        import logging

        client, _ = build_client()

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            post(client, message="look", attachments=[an_image()])

        assert self._lines(caplog)[0]["duration_ms"] >= 0

    def test_emits_a_rejected_event_for_a_bad_attachment(self, caplog):
        import logging

        client, _ = build_client()

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            resp = post(client, message="x", attachments=[an_image("image/heic")])

        assert resp.status_code == 400
        lines = self._lines(caplog)
        assert len(lines) == 1
        assert lines[0]["status"] == "rejected"
        assert lines[0]["reject_reason"]

    def test_flags_description_returned_false_when_the_model_omits_it(self, caplog):
        # D4 health signal.
        import logging

        client, _ = build_client(vision_payload=json.dumps({"reply": "Just a reply."}))

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            post(client, message="look", attachments=[an_image()])

        assert self._lines(caplog)[0]["description_returned"] is False

    def test_emits_nothing_for_a_text_only_turn(self, caplog):
        import logging

        client, _ = build_client()

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            post(client, message="just words")

        assert self._lines(caplog) == []

    def test_no_telemetry_line_contains_the_payload(self, caplog):
        import logging

        client, _ = build_client()

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            post(client, message="look", attachments=[an_image()])

        rendered = json.dumps(self._lines(caplog))
        assert JPEG_B64[:32] not in rendered
        assert "a lake at dusk" not in rendered  # description must not ship


class TestDailyCap:
    """Per-user daily vision cap, enforced before the OpenAI call."""

    def _patch_quota(self, side_effect=None):
        from unittest.mock import AsyncMock, patch

        return patch(
            "src.app.api.mirrorgpt_routes.get_chat_media_quota_service",
            return_value=type(
                "Q", (), {"consume_vision_turn": AsyncMock(side_effect=side_effect)}
            )(),
        )

    def test_a_turn_under_the_cap_proceeds(self):
        client, _ = build_client()

        with self._patch_quota():
            resp = post(client, message="look", attachments=[an_image()])

        assert resp.status_code == 200, resp.text

    def test_a_turn_over_the_cap_returns_429(self):
        from src.app.core.exceptions import RateLimitExceededError

        client, _ = build_client()

        with self._patch_quota(
            side_effect=RateLimitExceededError("too many photos today")
        ):
            resp = post(client, message="look", attachments=[an_image()])

        assert resp.status_code == 429, resp.text

    def test_the_cap_is_checked_BEFORE_the_openai_call(self):
        # Otherwise the cap costs money to enforce — the whole point is to
        # not spend on a turn the user isn't allowed.
        from src.app.core.exceptions import RateLimitExceededError

        client, _ = build_client()
        orchestrator = None
        from src.app.api.mirrorgpt_routes import get_mirror_orchestrator
        from src.app.handler import app

        orchestrator = app.dependency_overrides[get_mirror_orchestrator]()
        vision_call = (
            orchestrator.response_generator.openai_service.send_with_overrides_async
        )

        with self._patch_quota(side_effect=RateLimitExceededError("capped")):
            post(client, message="look", attachments=[an_image()])

        vision_call.assert_not_awaited()

    def test_a_text_only_turn_is_never_capped(self):
        # The cap is about vision spend. Text chat must be unaffected.
        from unittest.mock import AsyncMock, patch

        client, _ = build_client()
        consume = AsyncMock()

        with patch(
            "src.app.api.mirrorgpt_routes.get_chat_media_quota_service",
            return_value=type("Q", (), {"consume_vision_turn": consume})(),
        ):
            resp = post(client, message="just words")

        assert resp.status_code == 200, resp.text
        consume.assert_not_awaited()

    def test_a_capped_turn_is_recorded_in_telemetry(self, caplog):
        import logging

        from src.app.core.exceptions import RateLimitExceededError

        client, _ = build_client()

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            with self._patch_quota(side_effect=RateLimitExceededError("capped")):
                post(client, message="look", attachments=[an_image()])

        lines = [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == "telemetry.chat_media"
        ]
        assert len(lines) == 1
        assert lines[0]["status"] == "rejected"
        assert lines[0]["reject_reason"] == "daily_cap"
