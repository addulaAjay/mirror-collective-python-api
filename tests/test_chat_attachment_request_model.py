"""
Request-model contract for inline chat attachments (Phase 1).

The interesting case is the *image-only turn*: a user picks a photo, types
nothing, and sends. ``MirrorGPTChatRequest.message`` has carried
``min_length=1`` since the endpoint was written, so that turn is rejected
today. Relaxing it must stay conditional — an empty message with no
attachment is still meaningless and should still 422.
"""

import base64

import pytest
from pydantic import ValidationError as PydanticValidationError

from src.app.api.models import ChatAttachment, MirrorGPTChatRequest

JPEG_B64 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 32).decode()


def an_attachment(**overrides):
    payload = {
        "kind": "image",
        "mime_type": "image/jpeg",
        "data_base64": JPEG_B64,
    }
    payload.update(overrides)
    return payload


class TestTextOnlyBehaviourIsUnchanged:
    def test_a_plain_text_message_still_works(self):
        req = MirrorGPTChatRequest(message="hello mirror", session_id="s1")

        assert req.message == "hello mirror"
        assert req.attachments is None

    def test_an_empty_message_with_no_attachment_is_still_rejected(self):
        with pytest.raises(PydanticValidationError):
            MirrorGPTChatRequest(message="", session_id="s1")

    def test_a_whitespace_only_message_with_no_attachment_is_rejected(self):
        # Otherwise " " becomes a valid turn and the model is asked to reflect
        # on nothing.
        with pytest.raises(PydanticValidationError):
            MirrorGPTChatRequest(message="   ", session_id="s1")

    def test_the_5000_char_ceiling_still_applies(self):
        with pytest.raises(PydanticValidationError):
            MirrorGPTChatRequest(message="x" * 5001, session_id="s1")


class TestAttachmentTurns:
    def test_accepts_a_message_with_an_attachment(self):
        req = MirrorGPTChatRequest(
            message="what do you see?",
            session_id="s1",
            attachments=[an_attachment()],
        )

        assert req.attachments is not None
        assert len(req.attachments) == 1
        assert isinstance(req.attachments[0], ChatAttachment)
        assert req.attachments[0].mime_type == "image/jpeg"

    def test_accepts_an_image_with_NO_caption(self):
        # The headline new case: picking a photo and sending without typing.
        req = MirrorGPTChatRequest(
            message="",
            session_id="s1",
            attachments=[an_attachment()],
        )

        assert req.message == ""
        assert req.attachments is not None
        assert len(req.attachments) == 1

    def test_accepts_a_whitespace_caption_alongside_an_attachment(self):
        req = MirrorGPTChatRequest(
            message="  ",
            session_id="s1",
            attachments=[an_attachment()],
        )
        assert req.attachments is not None
        assert len(req.attachments) == 1

    def test_rejects_more_than_one_attachment_in_v1(self):
        # V1 is single-attachment. Enforced in the schema so the orchestrator
        # never has to decide what two images mean for one turn.
        with pytest.raises(PydanticValidationError):
            MirrorGPTChatRequest(
                message="two photos",
                session_id="s1",
                attachments=[an_attachment(), an_attachment()],
            )

    def test_rejects_an_empty_attachments_list_being_used_as_a_loophole(self):
        # attachments=[] must NOT license an empty message.
        with pytest.raises(PydanticValidationError):
            MirrorGPTChatRequest(message="", session_id="s1", attachments=[])

    def test_rejects_an_unknown_attachment_kind(self):
        with pytest.raises(PydanticValidationError):
            MirrorGPTChatRequest(
                message="a video",
                session_id="s1",
                attachments=[an_attachment(kind="video")],
            )

    def test_rejects_an_attachment_with_no_payload(self):
        with pytest.raises(PydanticValidationError):
            MirrorGPTChatRequest(
                message="empty",
                session_id="s1",
                attachments=[an_attachment(data_base64="")],
            )

    def test_rejects_an_attachment_with_no_mime_type(self):
        with pytest.raises(PydanticValidationError):
            MirrorGPTChatRequest(
                message="no mime",
                session_id="s1",
                attachments=[an_attachment(mime_type="")],
            )


class TestPayloadCeiling:
    def test_rejects_a_base64_payload_beyond_the_schema_ceiling(self):
        # A schema-level ceiling so an absurd payload is refused by pydantic
        # before any handler code runs. The precise byte check lives in
        # chat_attachment_service; this is the outer guard.
        with pytest.raises(PydanticValidationError):
            MirrorGPTChatRequest(
                message="huge",
                session_id="s1",
                attachments=[an_attachment(data_base64="A" * 10_000_000)],
            )


class TestValidationErrorsDoNotLeakThePayload:
    def test_a_failing_attachment_does_not_echo_base64_in_the_error(self):
        # Pydantic's error objects carry an `input` field. Our
        # validation_exception_handler builds its own {field, message} shape
        # and deliberately omits it — but the per-error `msg` must not embed
        # the value either, or the blob reaches logs and HTTP responses.
        big = "A" * 10_000_000
        with pytest.raises(PydanticValidationError) as exc:
            MirrorGPTChatRequest(
                message="huge",
                session_id="s1",
                attachments=[an_attachment(data_base64=big)],
            )

        for error in exc.value.errors():
            assert big[:64] not in error["msg"]
