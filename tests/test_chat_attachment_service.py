"""
Inline chat attachment decoding + validation (Phase 1 of the chat-media plan).

Chat media is sent inline as base64 and never stored (see the client repo's
docs/CHAT_MEDIA_V1_PLAN.md, D7). That makes this module the ONLY gate between
a caller-supplied blob and (a) the OpenAI vision call, (b) the /tmp file the
transcribe route writes. So the tests here lean hard on the hostile cases.
"""

import base64
from unittest.mock import patch

import pytest

from src.app.core.exceptions import ValidationError
from src.app.services.chat_attachment_service import (
    CHAT_AUDIO_MIME_TYPES,
    CHAT_IMAGE_MIME_TYPES,
    MAX_CHAT_AUDIO_BYTES,
    MAX_CHAT_IMAGE_BYTES,
    DecodedAttachment,
    decode_audio_attachment,
    decode_image_attachment,
)

# ── Minimal byte sequences that real decoders accept as file headers ──────────
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WEBP_BYTES = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"\x00" * 32
M4A_BYTES = b"\x00\x00\x00\x20" + b"ftypM4A " + b"\x00" * 32
AAC_BYTES = b"\xff\xf1" + b"\x00" * 32


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


# ── Happy paths ───────────────────────────────────────────────────────────────


class TestDecodeImageHappyPath:
    def test_decodes_a_jpeg(self):
        result = decode_image_attachment("image/jpeg", b64(JPEG_BYTES))

        assert isinstance(result, DecodedAttachment)
        assert result.kind == "image"
        assert result.mime_type == "image/jpeg"
        assert result.data == JPEG_BYTES
        assert result.size_bytes == len(JPEG_BYTES)

    def test_decodes_a_png(self):
        result = decode_image_attachment("image/png", b64(PNG_BYTES))
        assert result.mime_type == "image/png"
        assert result.data == PNG_BYTES

    def test_decodes_a_webp(self):
        result = decode_image_attachment("image/webp", b64(WEBP_BYTES))
        assert result.mime_type == "image/webp"

    def test_normalizes_mime_case_and_whitespace(self):
        result = decode_image_attachment("  IMAGE/JPEG  ", b64(JPEG_BYTES))
        assert result.mime_type == "image/jpeg"

    def test_accepts_a_data_url_prefix(self):
        # Belt and braces: a client that sends the full data: URL instead of
        # bare base64 should not get a confusing decode failure.
        payload = f"data:image/jpeg;base64,{b64(JPEG_BYTES)}"
        result = decode_image_attachment("image/jpeg", payload)
        assert result.data == JPEG_BYTES

    def test_to_data_url_round_trips_for_openai(self):
        result = decode_image_attachment("image/jpeg", b64(JPEG_BYTES))
        url = result.to_data_url()

        assert url.startswith("data:image/jpeg;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == JPEG_BYTES


# ── Hostile input ─────────────────────────────────────────────────────────────


class TestDecodeImageRejects:
    def test_rejects_a_disallowed_mime(self):
        # Deliberately narrower than Echo Vault's ALLOWED_UPLOAD_MIME_TYPES:
        # chat needs no video, no PDF, and no HEIC (the client converts HEIC
        # before sending).
        with pytest.raises(ValidationError) as exc:
            decode_image_attachment("image/heic", b64(JPEG_BYTES))
        assert "image/heic" in str(exc.value)

    def test_rejects_video_even_though_echo_vault_allows_it(self):
        with pytest.raises(ValidationError):
            decode_image_attachment("video/mp4", b64(JPEG_BYTES))

    def test_rejects_pdf(self):
        with pytest.raises(ValidationError):
            decode_image_attachment("application/pdf", b64(JPEG_BYTES))

    def test_rejects_malformed_base64(self):
        with pytest.raises(ValidationError) as exc:
            decode_image_attachment("image/jpeg", "!!!! not base64 !!!!")
        assert "base64" in str(exc.value).lower()

    def test_rejects_empty_payload(self):
        with pytest.raises(ValidationError):
            decode_image_attachment("image/jpeg", "")

    def test_rejects_a_payload_over_the_decoded_size_cap(self):
        oversized = b"\xff\xd8\xff\xe0" + b"\x00" * (MAX_CHAT_IMAGE_BYTES + 1)
        with pytest.raises(ValidationError) as exc:
            decode_image_attachment("image/jpeg", b64(oversized))
        assert "large" in str(exc.value).lower() or "size" in str(exc.value).lower()

    def test_rejects_oversized_input_WITHOUT_decoding_it(self):
        # The size check must run on the base64 length first. Decoding a
        # hostile 100MB payload just to discover it is too big hands an
        # attacker a memory-amplification primitive inside a 1769MB Lambda.
        huge_b64 = "A" * (MAX_CHAT_IMAGE_BYTES * 4)

        with patch(
            "src.app.services.chat_attachment_service.base64.b64decode"
        ) as mock_decode:
            with pytest.raises(ValidationError):
                decode_image_attachment("image/jpeg", huge_b64)

        mock_decode.assert_not_called()

    def test_rejects_mime_magic_byte_mismatch(self):
        # Caller claims JPEG but sends PNG bytes. Without this check the MIME
        # allowlist is decorative: anything could arrive labelled image/jpeg.
        with pytest.raises(ValidationError) as exc:
            decode_image_attachment("image/jpeg", b64(PNG_BYTES))
        assert "match" in str(exc.value).lower()

    def test_rejects_arbitrary_bytes_labelled_as_an_image(self):
        with pytest.raises(ValidationError):
            decode_image_attachment("image/png", b64(b"#!/bin/sh\nrm -rf /"))

    def test_rejects_a_truncated_header(self):
        with pytest.raises(ValidationError):
            decode_image_attachment("image/jpeg", b64(b"\xff\xd8"))

    def test_error_message_never_contains_the_payload(self):
        # The whole point of the redaction discipline: a validation message
        # ends up in logs and HTTP responses. It must never carry the blob.
        payload = b64(PNG_BYTES)
        with pytest.raises(ValidationError) as exc:
            decode_image_attachment("image/jpeg", payload)

        assert payload not in str(exc.value)
        assert payload[:32] not in str(exc.value)


# ── Audio (used by Phase 4's transcribe route) ────────────────────────────────


class TestDecodeAudio:
    def test_decodes_m4a(self):
        result = decode_audio_attachment("audio/m4a", b64(M4A_BYTES))

        assert result.kind == "audio"
        assert result.mime_type == "audio/m4a"
        assert result.data == M4A_BYTES

    def test_decodes_audio_mp4_as_the_same_container(self):
        result = decode_audio_attachment("audio/mp4", b64(M4A_BYTES))
        assert result.kind == "audio"

    def test_decodes_aac(self):
        result = decode_audio_attachment("audio/aac", b64(AAC_BYTES))
        assert result.mime_type == "audio/aac"

    def test_normalizes_the_x_m4a_alias(self):
        # Android content resolvers report audio/x-m4a.
        result = decode_audio_attachment("audio/x-m4a", b64(M4A_BYTES))
        assert result.mime_type == "audio/m4a"

    def test_rejects_an_image_mime(self):
        with pytest.raises(ValidationError):
            decode_audio_attachment("image/jpeg", b64(JPEG_BYTES))

    def test_rejects_audio_over_its_own_larger_cap(self):
        oversized = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * MAX_CHAT_AUDIO_BYTES
        with pytest.raises(ValidationError):
            decode_audio_attachment("audio/m4a", b64(oversized))

    def test_audio_cap_is_derived_from_the_recording_config(self):
        # 120s x 16kHz mono AAC (~32kbps) is ~480KB; the cap allows ~3x that
        # for a client recording at higher quality. An earlier revision
        # asserted "audio cap > image cap", which was the wrong basis —
        # speech audio is smaller than a detailed photo, not larger.
        expected_120s_bytes = 120 * 32_000 // 8
        assert MAX_CHAT_AUDIO_BYTES >= expected_120s_bytes * 2
        assert MAX_CHAT_AUDIO_BYTES <= expected_120s_bytes * 5

    def test_rejects_bytes_that_are_not_an_audio_container(self):
        with pytest.raises(ValidationError):
            decode_audio_attachment("audio/m4a", b64(b"just some text"))


# ── Module contract ───────────────────────────────────────────────────────────


class TestAllowlists:
    def test_image_allowlist_is_exactly_the_three_web_safe_types(self):
        assert CHAT_IMAGE_MIME_TYPES == frozenset(
            {"image/jpeg", "image/png", "image/webp"}
        )

    def test_audio_allowlist_covers_what_the_recorder_emits(self):
        assert "audio/m4a" in CHAT_AUDIO_MIME_TYPES
        assert "audio/mp4" in CHAT_AUDIO_MIME_TYPES

    def test_image_cap_leaves_headroom_under_the_lambda_payload_ceiling(self):
        # Lambda sync invoke caps at 6MB; base64 inflates by 4/3. The cap must
        # leave room for that inflation plus the rest of the chat request.
        assert MAX_CHAT_IMAGE_BYTES * 4 / 3 < 6 * 1024 * 1024

    def test_audio_cap_leaves_headroom_under_the_lambda_payload_ceiling(self):
        assert MAX_CHAT_AUDIO_BYTES * 4 / 3 < 6 * 1024 * 1024

    def test_decoded_attachment_is_immutable(self):
        # It crosses several layers (route → orchestrator → openai_service);
        # nothing downstream should be able to swap the bytes.
        result = decode_image_attachment("image/jpeg", b64(JPEG_BYTES))
        with pytest.raises(Exception):
            result.data = b"replaced"  # type: ignore[misc]

    def test_repr_does_not_leak_the_payload(self):
        # A DecodedAttachment may end up in an exception traceback or a debug
        # log line. Its repr must summarise, not dump.
        result = decode_image_attachment("image/jpeg", b64(JPEG_BYTES))
        rendered = repr(result)

        assert "image/jpeg" in rendered
        assert str(len(JPEG_BYTES)) in rendered
        # No base64, and no raw bytes literal.
        assert b64(JPEG_BYTES)[:24] not in rendered
        assert "\\xff\\xd8" not in rendered
