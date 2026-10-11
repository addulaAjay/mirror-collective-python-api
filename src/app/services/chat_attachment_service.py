"""
Inline chat attachment decoding and validation.

MirrorGPT chat media (a photo the user shows the Mirror, a voice note they
speak into it) travels **inline in the request body as base64** and is never
persisted — no S3 object, no DynamoDB bytes, no retention window. See the
client repo's ``docs/CHAT_MEDIA_V1_PLAN.md`` (D6, D7) for why this differs
from Echo Vault's presigned-upload pipeline: that machinery exists for 100MB
video, and a chat attachment is ~300KB.

Because nothing is stored, this module is the **only** gate between a
caller-supplied blob and the two things we do with it:

1. embed it in an OpenAI vision request as a ``data:`` URL;
2. write it to Lambda ``/tmp`` for Whisper to read.

So it validates in a deliberate order — cheapest and most protective first:

    MIME allowlist → size-from-base64-length → decode → exact size → magic bytes

The size check runs on the *base64 length* before decoding. Decoding a hostile
100MB payload only to discover it is too big would hand a caller a
memory-amplification primitive inside the Lambda.

Nothing in this module ever interpolates attachment bytes into an exception
message. Those messages reach both the HTTP response and CloudWatch.
"""

import base64
import binascii
from dataclasses import dataclass
from typing import Dict, Tuple

from ..core.exceptions import ValidationError

# ── Allowlists ────────────────────────────────────────────────────────────────

# Deliberately NARROWER than Echo Vault's ``ALLOWED_UPLOAD_MIME_TYPES``.
# Chat needs no video and no PDF, and no HEIC either: the client runs
# ``ensureWebSafePhoto`` (HEIC → JPEG) before sending, so a HEIC arriving here
# means the client skipped a step and we would rather fail loudly.
CHAT_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

# What ``react-native-audio-recorder-player`` actually emits. ``audio/mpeg``
# is excluded: nothing in the app produces mp3, and accepting a format we
# never generate only widens the surface.
CHAT_AUDIO_MIME_TYPES = frozenset({"audio/m4a", "audio/mp4", "audio/aac"})

# ── Size caps ─────────────────────────────────────────────────────────────────
#
# The binding constraint is Lambda's 6MB synchronous-invoke payload ceiling,
# against which base64 costs a 4/3 inflation. Both caps leave the majority of
# that ceiling free for the rest of the request.
#
# Image: a 1536px q.85 JPEG (the client's ``compressChatPhoto`` output) lands
# at 200-500KB, so 1.5MB is ~3x headroom for an unusually detailed photo.
MAX_CHAT_IMAGE_BYTES = 1_500_000

# Audio: derived from the recording config and the duration limit, NOT from
# comparison with the image cap (an earlier revision sized it that way, which
# was the wrong basis — a speech recording is *smaller* than a detailed photo,
# not larger).
#
#   120 s cap x 16 kHz mono AAC (~32 kbps) = ~480 KB
#   x3 headroom for a client recording at higher quality      = ~1.5 MB
#
# This byte cap — not a parsed container duration — is what actually bounds
# transcription cost server-side. Whisper bills per minute of audio, and at
# 1.5 MB the worst realistic case is a few minutes, i.e. a few cents. Parsing
# the MP4 `mvhd` atom for a true pre-call duration would be more precise, but
# the byte cap plus the per-user daily cap already bound spend, so it is not
# worth the parsing surface in V1. The real duration is reported in telemetry
# (from Whisper's verbose_json response) so we can check the assumption
# against actual usage rather than trusting it.
MAX_CHAT_AUDIO_BYTES = 1_500_000

# ── MIME normalization ────────────────────────────────────────────────────────

# Mirrors the client's src/utils/media/mime.ts so both ends agree on spelling.
_MIME_ALIASES: Dict[str, str] = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "audio/x-m4a": "audio/m4a",
}

# ── Magic-byte signatures ─────────────────────────────────────────────────────
#
# Without this check the MIME allowlist is decorative — a caller could label
# anything ``image/jpeg``. Each entry is (offset, expected bytes); all entries
# for a type must match.
_SIGNATURES: Dict[str, Tuple[Tuple[int, bytes], ...]] = {
    "image/jpeg": ((0, b"\xff\xd8\xff"),),
    "image/png": ((0, b"\x89PNG\r\n\x1a\n"),),
    # RIFF container with a WEBP form type at offset 8.
    "image/webp": ((0, b"RIFF"), (8, b"WEBP")),
    # ISO base media container (m4a/mp4) — 4-byte size, then 'ftyp'.
    "audio/m4a": ((4, b"ftyp"),),
    "audio/mp4": ((4, b"ftyp"),),
}

# The shortest prefix we need before any signature check is meaningful.
_MIN_SNIFF_BYTES = 12


@dataclass(frozen=True)
class DecodedAttachment:
    """A validated attachment, held in memory for the life of one request.

    Frozen because it crosses several layers (route → orchestrator →
    openai_service) and nothing downstream should be able to swap the bytes
    after validation has passed.
    """

    kind: str  # 'image' | 'audio'
    mime_type: str  # normalized, allowlisted
    data: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    def to_data_url(self) -> str:
        """Render as a ``data:`` URL for OpenAI's ``image_url`` content part.

        OpenAI accepts a base64 data URL directly, so the image never needs to
        be reachable from the public internet — which is what makes storing it
        unnecessary in the first place.
        """
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.mime_type};base64,{encoded}"

    def __repr__(self) -> str:
        """Summarise, never dump.

        The default dataclass repr would render the whole byte string. This
        object can legitimately appear in a traceback or a debug log line, so
        its repr reports shape only.
        """
        return (
            f"DecodedAttachment(kind={self.kind!r}, "
            f"mime_type={self.mime_type!r}, size_bytes={self.size_bytes})"
        )


def _normalize_mime(mime_type: str) -> str:
    cleaned = (mime_type or "").strip().lower()
    return _MIME_ALIASES.get(cleaned, cleaned)


def _strip_data_url_prefix(payload: str) -> str:
    """Tolerate a client that sends a full ``data:`` URL instead of bare base64.

    Cheap to accept and saves a confusing "invalid base64" failure.
    """
    if payload.startswith("data:") and "," in payload:
        return payload.split(",", 1)[1]
    return payload


def _decode(payload: str, max_bytes: int, label: str) -> bytes:
    """Decode base64 with the size check in front of the decode."""
    if not payload:
        raise ValidationError(f"Empty {label} attachment payload.")

    body = _strip_data_url_prefix(payload)
    if not body:
        raise ValidationError(f"Empty {label} attachment payload.")

    # Base64 encodes 3 bytes as 4 characters, so the decoded length is at most
    # len*3/4. Checking here means an oversized payload is rejected before it
    # is ever expanded in memory.
    if (len(body) * 3) // 4 > max_bytes:
        raise ValidationError(
            f"{label.capitalize()} attachment is too large. "
            f"Maximum size is {max_bytes} bytes."
        )

    try:
        data = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as err:
        # `err` is a codec message ("Invalid base64-encoded string..."), not
        # the payload — safe to surface. Never interpolate `payload`.
        raise ValidationError(
            f"Could not decode {label} attachment: malformed base64 ({err})."
        ) from err

    if not data:
        raise ValidationError(f"Empty {label} attachment payload.")

    # Exact check after decoding — the estimate above is an upper bound, and
    # padding means a payload can slip just under it.
    if len(data) > max_bytes:
        raise ValidationError(
            f"{label.capitalize()} attachment is too large. "
            f"Maximum size is {max_bytes} bytes."
        )

    return data


def _verify_signature(mime_type: str, data: bytes) -> None:
    """Confirm the bytes look like what the caller claims they are."""
    signature = _SIGNATURES.get(mime_type)

    if signature is None:
        # AAC (ADTS) has no fixed offset signature — frames begin with an
        # 11-bit sync word (0xFFE). Checked separately rather than left
        # unvalidated.
        if mime_type == "audio/aac":
            if len(data) < 2 or (data[0] != 0xFF) or (data[1] & 0xF0) != 0xF0:
                raise ValidationError(
                    "Attachment bytes do not match the declared type " f"'{mime_type}'."
                )
            return
        # An allowlisted type with no signature defined is a programming
        # error, not caller input — fail closed rather than waving it through.
        raise ValidationError(
            f"No content check available for declared type '{mime_type}'."
        )

    if len(data) < _MIN_SNIFF_BYTES:
        raise ValidationError(
            f"Attachment is too short to be a valid '{mime_type}' file."
        )

    for offset, expected in signature:
        if data[offset : offset + len(expected)] != expected:
            raise ValidationError(
                f"Attachment bytes do not match the declared type '{mime_type}'."
            )


def decode_image_attachment(mime_type: str, data_base64: str) -> DecodedAttachment:
    """Validate and decode an inline image attachment.

    Raises ``ValidationError`` (HTTP 400) for every rejection, with a message
    that is safe to log and to return to the caller.
    """
    normalized = _normalize_mime(mime_type)
    if normalized not in CHAT_IMAGE_MIME_TYPES:
        raise ValidationError(
            f"Unsupported image type '{normalized}'. "
            f"Allowed: {sorted(CHAT_IMAGE_MIME_TYPES)}"
        )

    data = _decode(data_base64, MAX_CHAT_IMAGE_BYTES, "image")
    _verify_signature(normalized, data)

    return DecodedAttachment(kind="image", mime_type=normalized, data=data)


def decode_audio_attachment(mime_type: str, data_base64: str) -> DecodedAttachment:
    """Validate and decode an inline audio attachment (transcription input)."""
    normalized = _normalize_mime(mime_type)
    if normalized not in CHAT_AUDIO_MIME_TYPES:
        raise ValidationError(
            f"Unsupported audio type '{normalized}'. "
            f"Allowed: {sorted(CHAT_AUDIO_MIME_TYPES)}"
        )

    data = _decode(data_base64, MAX_CHAT_AUDIO_BYTES, "audio")
    _verify_signature(normalized, data)

    return DecodedAttachment(kind="audio", mime_type=normalized, data=data)


# ── Persisting an attachment turn as text (D4) ────────────────────────────────

# The description is replayed into every prompt for the next 10 turns
# (``_get_conversation_history`` limit), so it is bounded. One sentence is the
# intent; this is the backstop against a model that ignores that.
_MAX_DESCRIPTION_CHARS = 300


def compose_attachment_turn_content(message: str, image_description: str) -> str:
    """Build the text that gets persisted for a turn carrying a photo.

    The image bytes are never stored (D7), so this composed string is the only
    lasting record of the turn. It has three readers, which is why the shape
    matters:

    1. ``_get_conversation_history`` replays it into later prompts, so the
       model can refer back to "the photo you showed me".
    2. ``archetype_engine.analyze_message`` is pure text regex — without words
       here, an image turn would contribute nothing to the 5-signal analysis.
    3. ``ConversationSummarizer`` folds it into the continuity summary.

    The bracketed marker keeps our description distinguishable from the user's
    own words, so the model does not attribute it to them.

    Always returns non-empty text, which is what lets
    ``_validate_message_inputs`` (non-empty content required) stay untouched
    for the no-caption case.
    """
    caption = (message or "").strip()

    # Collapse whitespace: a multiline description would corrupt the
    # newline-joined transcript the summarizer builds.
    description = " ".join((image_description or "").split())
    if len(description) > _MAX_DESCRIPTION_CHARS:
        description = description[:_MAX_DESCRIPTION_CHARS].rstrip() + "…"

    marker = f"[shared a photo: {description}]" if description else "[shared a photo]"

    if caption:
        return f"{caption}\n\n{marker}"
    return marker
