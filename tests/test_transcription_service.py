"""
Whisper transcription service (Phase 4).

Under D1 the transcript IS the message: the audio is transcribed in-request
and discarded, and the text goes back to the client for the user to read and
edit before they send it as an ordinary chat turn. Nothing about /chat
changes for voice.

Two things get the most attention here.

**The /tmp file must always be removed.** Lambda's /tmp persists across warm
invocations on the same container, so a leftover recording could outlive its
request on a container that later serves a different user. That makes the
unlink a correctness requirement, not tidiness.

**Nothing logs the audio or the transcript.** The transcript is the user's own
words about something personal, and it is already persisted as a chat message
via the normal /chat path — duplicating it into CloudWatch would widen
exposure for no diagnostic gain.
"""

import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.core.exceptions import InternalServerError
from src.app.services.chat_attachment_service import decode_audio_attachment
from src.app.services.transcription_service import STT_MODEL, TranscriptionService

M4A_BYTES = b"\x00\x00\x00\x20" + b"ftypM4A " + b"\x00" * 64


def an_attachment():
    import base64

    return decode_audio_attachment("audio/m4a", base64.b64encode(M4A_BYTES).decode())


def a_service(transcribe_result=None, side_effect=None):
    """Service with a mocked AsyncOpenAI client."""
    if transcribe_result is None:
        transcribe_result = MagicMock(
            text="I keep returning to the same door.", duration=12.5
        )

    client = MagicMock()
    client.audio = MagicMock()
    client.audio.transcriptions = MagicMock()
    client.audio.transcriptions.create = AsyncMock(
        return_value=transcribe_result, side_effect=side_effect
    )

    service = TranscriptionService()
    service._client = client  # type: ignore[attr-defined]
    return service, client


class TestHappyPath:
    async def test_returns_the_transcript_text(self):
        service, _ = a_service()

        result = await service.transcribe(an_attachment())

        assert result.text == "I keep returning to the same door."

    async def test_returns_the_audio_duration_when_whisper_reports_it(self):
        # Used for telemetry: it is the only way to check whether the byte cap
        # is actually bounding duration the way we assumed.
        service, _ = a_service()

        result = await service.transcribe(an_attachment())

        assert result.duration_ms == 12_500

    async def test_tolerates_a_response_with_no_duration(self):
        service, _ = a_service(transcribe_result=MagicMock(text="hello", spec=["text"]))

        result = await service.transcribe(an_attachment())

        assert result.text == "hello"
        assert result.duration_ms is None

    async def test_strips_surrounding_whitespace_from_the_transcript(self):
        service, _ = a_service(
            transcribe_result=MagicMock(text="  hello  ", duration=1.0)
        )

        result = await service.transcribe(an_attachment())

        assert result.text == "hello"

    async def test_uses_the_configured_model(self):
        service, client = a_service()

        await service.transcribe(an_attachment())

        assert client.audio.transcriptions.create.await_args.kwargs["model"] == (
            STT_MODEL
        )

    async def test_requests_verbose_json_so_duration_comes_back(self):
        service, client = a_service()

        await service.transcribe(an_attachment())

        kwargs = client.audio.transcriptions.create.await_args.kwargs
        assert kwargs["response_format"] == "verbose_json"


class TestTmpFileLifecycle:
    async def test_removes_the_tmp_file_after_a_successful_transcription(self):
        service, _ = a_service()

        with patch("os.unlink") as mock_unlink:
            await service.transcribe(an_attachment())

        assert mock_unlink.called

    async def test_removes_the_tmp_file_even_when_transcription_RAISES(self):
        # The important one. Lambda /tmp survives warm invocations, so a
        # leftover recording could outlive its request on a container that
        # later serves someone else.
        service, _ = a_service(side_effect=RuntimeError("openai down"))

        with patch("os.unlink") as mock_unlink:
            with pytest.raises(InternalServerError):
                await service.transcribe(an_attachment())

        assert mock_unlink.called

    async def test_a_failed_unlink_does_not_fail_the_request(self):
        # Best-effort cleanup: the user already has their transcript.
        service, _ = a_service()

        with patch("os.unlink", side_effect=OSError("busy")):
            result = await service.transcribe(an_attachment())

        assert result.text

    async def test_the_tmp_file_is_written_under_tmp(self):
        service, _ = a_service()
        written: list = []

        real_open = open

        def spy_open(path, *args, **kwargs):
            written.append(str(path))
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=spy_open):
            await service.transcribe(an_attachment())

        assert any(p.startswith("/tmp/") for p in written)

    async def test_each_call_uses_a_distinct_filename(self):
        # Two concurrent invocations in the same warm container must not write
        # over each other's audio.
        service, _ = a_service()
        paths: list = []

        real_open = open

        def spy_open(path, *args, **kwargs):
            paths.append(str(path))
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=spy_open):
            await service.transcribe(an_attachment())
            await service.transcribe(an_attachment())

        # Each call opens its path twice (write, then read), so duplicates
        # are expected — what matters is that the two CALLS used different
        # paths.
        tmp_paths = {p for p in paths if p.startswith("/tmp/")}
        assert len(tmp_paths) == 2


class TestFailureHandling:
    async def test_wraps_an_openai_failure_in_a_typed_error(self):
        service, _ = a_service(side_effect=RuntimeError("boom"))

        with pytest.raises(InternalServerError):
            await service.transcribe(an_attachment())

    async def test_an_empty_transcript_is_surfaced_as_empty_not_as_an_error(self):
        # Silence, or a recording of nothing, is a legitimate outcome. The
        # route decides how to present it; this layer does not invent text.
        service, _ = a_service(transcribe_result=MagicMock(text="", duration=0.5))

        result = await service.transcribe(an_attachment())

        assert result.text == ""


class TestNeverLogsContent:
    async def test_logs_contain_no_transcript_text(self, caplog):
        service, _ = a_service()

        with caplog.at_level(logging.DEBUG):
            await service.transcribe(an_attachment())

        rendered = " ".join(r.getMessage() for r in caplog.records)
        assert "I keep returning" not in rendered

    async def test_logs_contain_no_audio_bytes(self, caplog):
        service, _ = a_service()

        with caplog.at_level(logging.DEBUG):
            await service.transcribe(an_attachment())

        rendered = " ".join(r.getMessage() for r in caplog.records)
        assert "ftypM4A" not in rendered

    async def test_an_openai_failure_log_contains_no_audio(self, caplog):
        service, _ = a_service(side_effect=RuntimeError("boom"))

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(InternalServerError):
                await service.transcribe(an_attachment())

        rendered = " ".join(r.getMessage() for r in caplog.records)
        assert "ftypM4A" not in rendered


class TestConfiguration:
    def test_defaults_to_whisper_1(self):
        # The cost floor. gpt-4o-transcribe is more accurate on reflective,
        # halting speech but needs a real benchmark first (plan §9.3).
        assert STT_MODEL == os.getenv("MIRRORGPT_STT_MODEL", "whisper-1")

    def test_the_client_timeout_stays_under_the_gateway_ceiling(self):
        # API Gateway caps the integration at 30s. A client timeout at or
        # above that would mean the gateway 504s before OpenAI gives up, so
        # we would never see the real error.
        from src.app.services.transcription_service import STT_TIMEOUT_SECONDS

        assert STT_TIMEOUT_SECONDS < 30

    def test_the_client_timeout_is_longer_than_the_shared_chat_timeout(self):
        # The shared OpenAI client uses timeout=20.0, tuned for short
        # completions. A 120s recording can legitimately take longer to
        # transcribe than a chat reply takes to generate.
        from src.app.services.transcription_service import STT_TIMEOUT_SECONDS

        assert STT_TIMEOUT_SECONDS > 20
