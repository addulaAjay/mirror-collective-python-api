"""
Vision response generation (Phase 2 of the chat-media plan).

Two things are being pinned here.

**The content-parts payload.** An image turn must reach OpenAI as a
content-parts list with a ``data:`` URL, at ``detail: 'low'``.

**D4 — vision runs once.** The reply and a one-line image description come
back from a SINGLE completion via JSON mode. A second call would re-send the
image and re-pay the full image cost, and would add a round-trip to a path
already pressed against the 30s API Gateway ceiling. The description is what
gets persisted, so subsequent turns replay text and never the image.
"""

import base64
import json
from unittest.mock import AsyncMock

import pytest

from src.app.services.chat_attachment_service import decode_image_attachment
from src.app.services.mirror_orchestrator import ResponseGenerator, VisionResponse

JPEG_B64 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 32).decode()

ANALYSIS = {
    "signal_1_emotional_resonance": {
        "dominant_emotion": "longing",
        "valence": 0.2,
        "arousal": 0.4,
    },
    "signal_2_symbolic_language": {"extracted_symbols": ["water", "door"]},
    "signal_3_archetype_blend": {
        "primary": "Seeker",
        "secondary": "Weaver",
        "confidence": 0.7,
    },
}
NO_CHANGES: dict = {"changes": [], "archetype_shift_detected": False}


@pytest.fixture
def attachment():
    return decode_image_attachment("image/jpeg", JPEG_B64)


def a_generator(reply="A still surface.", description="a quiet lake at dusk"):
    """ResponseGenerator whose OpenAI call returns a valid JSON-mode payload."""
    service = AsyncMock()
    service.send_with_overrides_async = AsyncMock(
        return_value=json.dumps({"reply": reply, "image_description": description})
    )
    return ResponseGenerator(service), service


class TestPayloadSentToOpenAI:
    async def test_sends_text_and_image_as_content_parts(self, attachment):
        gen, service = a_generator()

        await gen.generate_vision_response(
            user_message="what do you see?",
            attachments=[attachment],
            analysis_result=ANALYSIS,
            change_analysis=NO_CHANGES,
        )

        messages = service.send_with_overrides_async.call_args.kwargs["messages"]
        user_msg = messages[-1]
        assert user_msg.role == "user"
        assert isinstance(user_msg.content, list)

        types = [p["type"] for p in user_msg.content]
        assert "text" in types
        assert "image_url" in types

    async def test_embeds_the_image_as_a_data_url(self, attachment):
        gen, service = a_generator()

        await gen.generate_vision_response(
            user_message="look",
            attachments=[attachment],
            analysis_result=ANALYSIS,
            change_analysis=NO_CHANGES,
        )

        content = service.send_with_overrides_async.call_args.kwargs["messages"][
            -1
        ].content
        image_part = next(p for p in content if p["type"] == "image_url")

        url = image_part["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == attachment.data

    async def test_requests_low_detail(self, attachment):
        # Fixes image cost flat regardless of dimensions. The Mirror responds
        # to emotional/symbolic content, not fine detail.
        gen, service = a_generator()

        await gen.generate_vision_response(
            user_message="look",
            attachments=[attachment],
            analysis_result=ANALYSIS,
            change_analysis=NO_CHANGES,
        )

        content = service.send_with_overrides_async.call_args.kwargs["messages"][
            -1
        ].content
        image_part = next(p for p in content if p["type"] == "image_url")
        assert image_part["image_url"]["detail"] == "low"

    async def test_uses_json_mode(self, attachment):
        gen, service = a_generator()

        await gen.generate_vision_response(
            user_message="look",
            attachments=[attachment],
            analysis_result=ANALYSIS,
            change_analysis=NO_CHANGES,
        )

        kwargs = service.send_with_overrides_async.call_args.kwargs
        assert kwargs["response_format"] == {"type": "json_object"}

    async def test_system_prompt_mentions_json(self, attachment):
        # OpenAI rejects json_object mode unless the word "json" appears in
        # the prompt. Easy to break by editing the prompt later.
        gen, service = a_generator()

        await gen.generate_vision_response(
            user_message="look",
            attachments=[attachment],
            analysis_result=ANALYSIS,
            change_analysis=NO_CHANGES,
        )

        system_msg = service.send_with_overrides_async.call_args.kwargs["messages"][0]
        assert "json" in system_msg.content.lower()

    async def test_makes_exactly_one_openai_call(self, attachment):
        # The D4 guarantee. A second call would re-send the image.
        gen, service = a_generator()

        await gen.generate_vision_response(
            user_message="look",
            attachments=[attachment],
            analysis_result=ANALYSIS,
            change_analysis=NO_CHANGES,
        )

        assert service.send_with_overrides_async.await_count == 1

    async def test_history_is_passed_through_as_text_only(self, attachment):
        from src.app.services.openai_service import ChatMessage

        gen, service = a_generator()
        history = [
            ChatMessage("user", "earlier thing"),
            ChatMessage("assistant", "earlier reply"),
        ]

        await gen.generate_vision_response(
            user_message="look",
            attachments=[attachment],
            analysis_result=ANALYSIS,
            change_analysis=NO_CHANGES,
            history=history,
        )

        messages = service.send_with_overrides_async.call_args.kwargs["messages"]
        # Every message except the final user turn carries string content —
        # history must never be re-inflated with image parts.
        for msg in messages[:-1]:
            assert isinstance(msg.content, str)

    async def test_image_only_turn_still_sends_an_image_part(self, attachment):
        gen, service = a_generator()

        await gen.generate_vision_response(
            user_message="",
            attachments=[attachment],
            analysis_result=ANALYSIS,
            change_analysis=NO_CHANGES,
        )

        content = service.send_with_overrides_async.call_args.kwargs["messages"][
            -1
        ].content
        assert any(p["type"] == "image_url" for p in content)


class TestParsingTheResponse:
    async def test_returns_reply_and_description(self, attachment):
        gen, _ = a_generator(reply="A still surface.", description="a lake at dusk")

        result = await gen.generate_vision_response(
            user_message="look",
            attachments=[attachment],
            analysis_result=ANALYSIS,
            change_analysis=NO_CHANGES,
        )

        assert isinstance(result, VisionResponse)
        assert result.reply == "A still surface."
        assert result.image_description == "a lake at dusk"

    async def test_tolerates_malformed_json_without_losing_the_reply(self, attachment):
        # If the model ignores JSON mode, a plain-text reply is still far
        # better than an error — the user sent a photo and deserves an answer.
        service = AsyncMock()
        service.send_with_overrides_async = AsyncMock(
            return_value="I see a quiet lake."
        )
        gen = ResponseGenerator(service)

        result = await gen.generate_vision_response(
            user_message="look",
            attachments=[attachment],
            analysis_result=ANALYSIS,
            change_analysis=NO_CHANGES,
        )

        assert result.reply == "I see a quiet lake."
        # No description parsed, so persistence falls back to a generic label
        # rather than inventing one.
        assert result.image_description == ""

    async def test_tolerates_json_missing_the_description_key(self, attachment):
        service = AsyncMock()
        service.send_with_overrides_async = AsyncMock(
            return_value=json.dumps({"reply": "Only a reply."})
        )
        gen = ResponseGenerator(service)

        result = await gen.generate_vision_response(
            user_message="look",
            attachments=[attachment],
            analysis_result=ANALYSIS,
            change_analysis=NO_CHANGES,
        )

        assert result.reply == "Only a reply."
        assert result.image_description == ""

    async def test_raises_nothing_and_degrades_when_openai_fails(self, attachment):
        # Mirrors generate_enhanced_response's existing behaviour: fall back
        # to the template response rather than 500 the chat turn.
        service = AsyncMock()
        service.send_with_overrides_async = AsyncMock(
            side_effect=RuntimeError("openai down")
        )
        gen = ResponseGenerator(service)

        result = await gen.generate_vision_response(
            user_message="look",
            attachments=[attachment],
            analysis_result=ANALYSIS,
            change_analysis=NO_CHANGES,
        )

        assert isinstance(result, VisionResponse)
        assert result.reply  # non-empty fallback
        assert result.image_description == ""
