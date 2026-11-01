"""
``ChatMessage`` widening for vision content parts (Phase 2).

``ChatMessage.content`` was ``str``-only, which is the single hard blocker for
sending an image to OpenAI. Widening it to accept a content-parts list is the
highest-blast-radius change in the chat-media plan: the class is used by the
orchestrator, the summarizer, soul pings, life-anchor structuring and the
greeting generator.

So the bulk of this file is not about the new capability — it is about pinning
that every existing string caller is byte-for-byte unaffected.
"""

import base64
from typing import Any, Dict, List

import pytest

from src.app.services.chat_attachment_service import decode_image_attachment
from src.app.services.openai_service import ChatMessage

JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
JPEG_B64 = base64.b64encode(JPEG_BYTES).decode()


class TestStringContentIsUnchanged:
    """Regression guard for the 16 existing call sites."""

    def test_a_plain_string_message_still_round_trips(self):
        msg = ChatMessage("user", "hello mirror")

        assert msg.role == "user"
        assert msg.content == "hello mirror"
        assert msg.to_dict() == {"role": "user", "content": "hello mirror"}

    @pytest.mark.parametrize("role", ["system", "user", "assistant"])
    def test_every_valid_role_still_works(self, role):
        assert ChatMessage(role, "text").to_dict() == {"role": role, "content": "text"}

    def test_an_invalid_role_is_still_rejected(self):
        with pytest.raises(ValueError, match="Invalid message role"):
            ChatMessage("tool", "text")

    def test_an_invalid_role_is_rejected_for_list_content_too(self):
        # The new content shape must not become a way around role validation.
        with pytest.raises(ValueError, match="Invalid message role"):
            ChatMessage("tool", [{"type": "text", "text": "x"}])

    def test_an_empty_string_is_still_permitted(self):
        # Callers (e.g. the summarizer on an empty transcript) rely on this;
        # ChatMessage has never enforced non-empty content.
        assert ChatMessage("user", "").to_dict() == {"role": "user", "content": ""}

    def test_to_dict_returns_a_plain_dict_openai_accepts(self):
        result = ChatMessage("system", "prompt").to_dict()

        assert isinstance(result, dict)
        assert set(result.keys()) == {"role", "content"}


class TestContentPartsForVision:
    def test_accepts_a_text_plus_image_content_list(self):
        parts: List[Dict[str, Any]] = [
            {"type": "text", "text": "what do you see?"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{JPEG_B64}"},
            },
        ]
        msg = ChatMessage("user", parts)

        assert msg.to_dict() == {"role": "user", "content": parts}

    def test_passes_the_content_list_through_untouched(self):
        # openai_service must not reshape, reorder or re-encode the parts —
        # the orchestrator owns that structure.
        parts: List[Dict[str, Any]] = [
            {"type": "text", "text": "a"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA", "detail": "low"},
            },
        ]
        out = ChatMessage("user", parts).to_dict()

        assert out["content"] is parts
        assert out["content"][1]["image_url"]["detail"] == "low"

    def test_a_decoded_attachment_composes_into_a_valid_content_part(self):
        # End-to-end of the two new pieces: the service decodes, the message
        # carries it.
        attachment = decode_image_attachment("image/jpeg", JPEG_B64)
        msg = ChatMessage(
            "user",
            [
                {"type": "text", "text": "look"},
                {
                    "type": "image_url",
                    "image_url": {"url": attachment.to_data_url(), "detail": "low"},
                },
            ],
        )

        content = msg.to_dict()["content"]
        assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_an_image_only_content_list_is_valid(self):
        # The no-caption turn: an image part with no text part.
        parts = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{JPEG_B64}"},
            }
        ]
        assert ChatMessage("user", parts).to_dict()["content"] == parts


class TestRejectsNonsenseContent:
    @pytest.mark.parametrize("bad", [None, 42, 3.5, True, {"type": "text"}])
    def test_rejects_content_that_is_neither_str_nor_list(self, bad):
        # Catching this at construction keeps the failure next to the bug
        # rather than inside an OpenAI 400 that is hard to trace back.
        with pytest.raises((TypeError, ValueError)):
            ChatMessage("user", bad)

    def test_rejects_an_empty_content_list(self):
        # An empty parts list produces an OpenAI 400; fail locally instead.
        with pytest.raises(ValueError):
            ChatMessage("user", [])

    def test_rejects_list_items_that_are_not_dicts(self):
        with pytest.raises((TypeError, ValueError)):
            ChatMessage("user", ["just a string"])  # type: ignore[list-item]


class TestReprDoesNotDumpImageBytes:
    def test_str_and_repr_stay_bounded_for_image_content(self):
        # A ChatMessage carrying a data URL can reach a traceback or a debug
        # log. A 400KB base64 string in a CloudWatch line is both a cost and a
        # privacy problem (plan §8).
        attachment = decode_image_attachment("image/jpeg", JPEG_B64)
        msg = ChatMessage(
            "user",
            [
                {
                    "type": "image_url",
                    "image_url": {"url": attachment.to_data_url()},
                }
            ],
        )

        rendered = f"{msg!r} {msg!s}"
        assert JPEG_B64[:24] not in rendered
