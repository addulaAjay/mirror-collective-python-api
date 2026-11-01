"""
End-to-end: POST /api/mirrorgpt/transcribe.

Phase 4 of the chat-media plan. Voice input is transcribe-to-text (D1): the
client records, posts the audio inline, gets text back, and the user reads and
edits it before sending as an ordinary chat turn. So this route's whole job is
audio in → text out, with nothing stored and nothing logged.

It is a separate endpoint from /chat on purpose (D5): /chat already runs
p95 25-35s against a 30s API Gateway integration ceiling, so chaining
transcription in front of it would produce 504s.
"""

import base64
import json
import os
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

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

M4A_BYTES = b"\x00\x00\x00\x20" + b"ftypM4A " + b"\x00" * 128
M4A_B64 = base64.b64encode(M4A_BYTES).decode()
JPEG_B64 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 32).decode()


def build_client(text="I keep circling the same door.", duration_ms=12_500):
    from src.app.core.enhanced_auth import get_user_with_profile
    from src.app.handler import app
    from src.app.services.transcription_service import Transcription

    async def fake_user():
        return {"id": "test-user-123", "email": "t@example.com", "fullName": "Test"}

    app.dependency_overrides[get_user_with_profile] = fake_user

    service = MagicMock()
    service.transcribe = AsyncMock(
        return_value=Transcription(text=text, duration_ms=duration_ms)
    )
    return TestClient(app), service


def teardown_function():
    from src.app.handler import app

    app.dependency_overrides.clear()


def post(client, service, quota_error=None, **body):
    """POST with the transcription service and quota patched at the route."""
    payload: Dict[str, Any] = {"mime_type": "audio/m4a", "data_base64": M4A_B64}
    payload.update(body)

    quota = MagicMock()
    quota.consume_transcription_turn = AsyncMock(side_effect=quota_error)

    with (
        patch(
            "src.app.api.mirrorgpt_routes.get_transcription_service",
            return_value=service,
        ),
        patch(
            "src.app.api.mirrorgpt_routes.get_chat_media_quota_service",
            return_value=quota,
        ),
    ):
        response = client.post(
            "/api/mirrorgpt/transcribe",
            json=payload,
            headers={"Authorization": "Bearer test-token"},
        )
    return response, quota


class TestHappyPath:
    def test_returns_the_transcript(self):
        client, service = build_client()

        resp, _ = post(client, service)

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["text"] == "I keep circling the same door."

    def test_returns_the_duration(self):
        client, service = build_client()

        resp, _ = post(client, service)

        assert resp.json()["data"]["duration_ms"] == 12_500

    def test_accepts_audio_mp4_as_the_same_container(self):
        client, service = build_client()

        resp, _ = post(client, service, mime_type="audio/mp4")

        assert resp.status_code == 200, resp.text

    def test_an_empty_transcript_is_a_200_not_an_error(self):
        # Silence is a legitimate outcome — the client shows "we didn't catch
        # that" rather than an error state.
        client, service = build_client(text="")

        resp, _ = post(client, service)

        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["text"] == ""


class TestRejections:
    def test_rejects_an_image_mime_with_400(self):
        client, service = build_client()

        resp, _ = post(client, service, mime_type="image/jpeg", data_base64=JPEG_B64)

        assert resp.status_code == 400, resp.text

    def test_rejects_malformed_base64_with_400(self):
        client, service = build_client()

        resp, _ = post(client, service, data_base64="!!!not base64!!!")

        assert resp.status_code == 400, resp.text

    def test_rejects_bytes_that_are_not_an_audio_container_with_400(self):
        client, service = build_client()

        resp, _ = post(
            client, service, data_base64=base64.b64encode(b"plain text").decode()
        )

        assert resp.status_code == 400, resp.text

    def test_rejects_a_missing_payload_with_422(self):
        client, service = build_client()

        resp, _ = post(client, service, data_base64="")

        assert resp.status_code == 422, resp.text

    def test_does_not_call_openai_for_a_rejected_payload(self):
        # The point of validating first: a bad payload must cost nothing.
        client, service = build_client()

        post(client, service, mime_type="image/jpeg", data_base64=JPEG_B64)

        service.transcribe.assert_not_awaited()

    def test_a_rejection_never_echoes_the_payload(self):
        client, service = build_client()

        resp, _ = post(client, service, mime_type="image/jpeg", data_base64=JPEG_B64)

        assert M4A_B64[:32] not in resp.text
        assert JPEG_B64[:32] not in resp.text


class TestDailyCap:
    def test_consumes_the_transcription_allowance(self):
        client, service = build_client()

        _, quota = post(client, service)

        quota.consume_transcription_turn.assert_awaited_once()

    def test_returns_429_when_over_the_cap(self):
        from src.app.core.exceptions import RateLimitExceededError

        client, service = build_client()

        resp, _ = post(client, service, quota_error=RateLimitExceededError("too many"))

        assert resp.status_code == 429, resp.text

    def test_does_not_call_openai_when_over_the_cap(self):
        # Transcription is the largest cost lever in the feature; enforcing
        # after the call would mean paying for every turn we then refuse.
        from src.app.core.exceptions import RateLimitExceededError

        client, service = build_client()

        post(client, service, quota_error=RateLimitExceededError("too many"))

        service.transcribe.assert_not_awaited()


class TestFailureHandling:
    def test_a_transcription_failure_returns_500_not_a_silent_empty_string(self):
        from src.app.core.exceptions import InternalServerError

        client, service = build_client()
        service.transcribe = AsyncMock(
            side_effect=InternalServerError("Could not transcribe that recording.")
        )

        resp, _ = post(client, service)

        assert resp.status_code == 500, resp.text

    def test_a_failure_response_contains_no_audio(self):
        from src.app.core.exceptions import InternalServerError

        client, service = build_client()
        service.transcribe = AsyncMock(side_effect=InternalServerError("boom"))

        resp, _ = post(client, service)

        assert M4A_B64[:32] not in resp.text


class TestTelemetry:
    def _lines(self, caplog):
        return [
            json.loads(r.getMessage())
            for r in caplog.records
            if r.name == "telemetry.chat_media"
        ]

    def test_emits_a_success_event_with_kind_audio(self, caplog):
        import logging

        client, service = build_client()

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            post(client, service)

        lines = self._lines(caplog)
        assert len(lines) == 1
        assert lines[0]["status"] == "success"
        assert lines[0]["kind"] == "audio"
        assert lines[0]["decoded_bytes"] == len(M4A_BYTES)

    def test_reports_the_real_audio_duration(self, caplog):
        # The one way to check whether the byte cap bounds duration the way
        # the plan assumed, since we don't parse the container ourselves.
        import logging

        client, service = build_client(duration_ms=48_000)

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            post(client, service)

        assert self._lines(caplog)[0]["audio_duration_ms"] == 48_000

    def test_emits_a_rejected_event_for_a_bad_payload(self, caplog):
        import logging

        client, service = build_client()

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            post(client, service, mime_type="image/jpeg", data_base64=JPEG_B64)

        lines = self._lines(caplog)
        assert len(lines) == 1
        assert lines[0]["status"] == "rejected"

    def test_emits_a_rejected_event_with_a_daily_cap_reason(self, caplog):
        import logging

        from src.app.core.exceptions import RateLimitExceededError

        client, service = build_client()

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            post(client, service, quota_error=RateLimitExceededError("x"))

        assert self._lines(caplog)[0]["reject_reason"] == "daily_cap"

    def test_no_telemetry_line_contains_the_transcript_or_audio(self, caplog):
        import logging

        client, service = build_client()

        with caplog.at_level(logging.INFO, logger="telemetry.chat_media"):
            post(client, service)

        rendered = json.dumps(self._lines(caplog))
        assert "same door" not in rendered
        assert M4A_B64[:32] not in rendered
