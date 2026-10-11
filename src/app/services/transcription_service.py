"""Speech-to-text for MirrorGPT voice input.

Under D1 of the chat-media plan the **transcript is the message**: a recording
is transcribed in-request, the text goes back to the client for the user to
read and edit, and they then send it as an ordinary chat turn. Nothing
downstream of ``/chat`` changes for voice — history, continuity summaries,
archetype analysis, Life Anchors and Soul Pings all keep working because they
only ever see text.

Two consequences shape this module.

**The recording is never stored.** It exists in the request body, in ``/tmp``
for the duration of one invocation, and nowhere else. Lambda's ``/tmp``
persists across warm invocations on the same container, so the unlink is a
correctness requirement rather than tidiness — a leftover recording could
otherwise outlive its request on a container that later serves a different
user. Hence the ``finally``.

**Nothing here logs the audio or the transcript.** The transcript is the
user's own words about something personal, and it is already persisted as a
chat message via the normal ``/chat`` path. Duplicating it into CloudWatch
would widen exposure for no diagnostic gain. Only shape is logged.

Why a separate OpenAI client
----------------------------
``OpenAIService`` pins ``timeout=20.0``, tuned for short chat completions. A
120 s recording can legitimately take longer to transcribe than a reply takes
to generate, so this uses its own client with a longer timeout — while
staying under the 30 s API Gateway integration ceiling, past which the gateway
504s and we never see the real error.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from openai import AsyncOpenAI

from ..core.exceptions import InternalServerError
from .chat_attachment_service import DecodedAttachment

logger = logging.getLogger(__name__)

# whisper-1 is the cost floor and the default. gpt-4o-transcribe is reportedly
# better on reflective, halting speech — which is exactly what this product
# invites — but it needs a real benchmark on sample recordings before it
# becomes the default (plan §9.3).
STT_MODEL = os.getenv("MIRRORGPT_STT_MODEL", "whisper-1")

# Longer than the shared 20 s chat client, shorter than the 30 s gateway cap.
STT_TIMEOUT_SECONDS = float(os.getenv("MIRRORGPT_STT_TIMEOUT_SECONDS", "25"))

# Canonical extension per accepted MIME. Whisper infers the container from the
# filename, so this has to be right or a valid recording is rejected as an
# unsupported format.
_EXTENSION_BY_MIME = {
    "audio/m4a": "m4a",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
}


@dataclass(frozen=True)
class Transcription:
    """Result of one transcription."""

    text: str
    # Audio length as reported by Whisper. Telemetry-only: it is the one way
    # to check whether the byte cap is bounding duration the way the plan
    # assumed, since we deliberately don't parse the container ourselves.
    # None when the model's response omits it.
    duration_ms: Optional[int] = None


class TranscriptionService:
    """Transcribe a decoded audio attachment to text."""

    def __init__(self) -> None:
        self._client: Optional[AsyncOpenAI] = None

    def _get_client(self) -> AsyncOpenAI:
        """Lazily build the client.

        Lazy so importing this module doesn't require OPENAI_API_KEY — the
        same reason the rest of the codebase defers client construction.
        """
        if self._client is None:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise InternalServerError("OPENAI_API_KEY is not configured")
            self._client = AsyncOpenAI(
                api_key=api_key,
                timeout=STT_TIMEOUT_SECONDS,
                # One retry only. A retry re-uploads the audio and re-spends
                # the clock against a 30 s ceiling, so a second one would be
                # more likely to produce a gateway 504 than a transcript.
                max_retries=1,
            )
        return self._client

    async def transcribe(self, attachment: DecodedAttachment) -> Transcription:
        """Transcribe audio, then delete it.

        Raises:
            InternalServerError: transcription failed. The caller decides how
                to present that; the recording is already gone either way.
        """
        extension = _EXTENSION_BY_MIME.get(attachment.mime_type, "m4a")
        # uuid4 rather than a fixed name: two concurrent invocations in the
        # same warm container must not write over each other's audio.
        path = f"/tmp/mc-voice-{uuid.uuid4().hex}.{extension}"  # nosec B108

        try:
            with open(path, "wb") as handle:
                handle.write(attachment.data)

            logger.info(
                "transcribe: mime=%s bytes=%d model=%s",
                attachment.mime_type,
                attachment.size_bytes,
                STT_MODEL,
            )

            with open(path, "rb") as handle:
                response: Any = await self._get_client().audio.transcriptions.create(
                    model=STT_MODEL,
                    file=handle,
                    # verbose_json so the response carries `duration`. Plain
                    # json omits it, and we want it for telemetry.
                    response_format="verbose_json",
                    # The product is English-only today. Pinning the language
                    # stops Whisper mis-detecting a halting or heavily
                    # accented recording as another language and "translating"
                    # it into nonsense.
                    language="en",
                )

            return Transcription(
                text=(getattr(response, "text", "") or "").strip(),
                duration_ms=_duration_ms(response),
            )

        except Exception as err:
            # `err` from the OpenAI SDK describes the API failure, not the
            # audio — safe to log. Never log `attachment.data`.
            logger.error("transcription failed: %s", err)
            raise InternalServerError("Could not transcribe that recording.") from err

        finally:
            # Always, including on failure. See the module docstring on
            # Lambda /tmp surviving warm invocations.
            try:
                os.unlink(path)
            except OSError as err:
                # Best-effort: the user already has (or has lost) their
                # transcript, and failing the request over cleanup would be
                # the wrong trade. Logged so a leak is visible.
                logger.warning("could not remove temp audio file: %s", err)


def _duration_ms(response: Any) -> Optional[int]:
    """Pull the duration out of a verbose_json response, if present."""
    duration = getattr(response, "duration", None)
    if duration is None:
        return None
    try:
        return int(float(duration) * 1000)
    except (TypeError, ValueError):
        return None


_transcription_service: Optional[TranscriptionService] = None


def get_transcription_service() -> TranscriptionService:
    """Module-level singleton, so the client is reused per container."""
    global _transcription_service
    if _transcription_service is None:
        _transcription_service = TranscriptionService()
    return _transcription_service
