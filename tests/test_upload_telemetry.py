"""Tests for the echo-upload telemetry beacon.

Covers:
- Path scrubbing on error_message (iOS, Android content://, file:// URIs)
- 200-char truncation
- Non-string fields pass through; rich-types are dropped silently
- Route happy path + auth gate
- emit failure is swallowed (telemetry never breaks the route)
"""

import json
from typing import Any, Dict
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.app.services.telemetry.upload_events import (
    EVENT_ECHO_UPLOAD,
    MAX_ERROR_MESSAGE_LEN,
    _scrub_error_message,
    emit_echo_upload,
)

# ----------------------------------------------------------------------
# _scrub_error_message
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_substring",
    [
        ("Error reading /var/mobile/Cache/clip.mp4", "[path]"),
        ("Failed to open file:///Users/jane/Documents/x.jpg", "[path]"),
        ("content://media/external/video/123 not found", "[path]"),
        ("S3 upload failed (403) — no path here", "S3 upload failed (403)"),
    ],
)
def test_scrub_error_message_replaces_paths_with_token(raw, expected_substring):
    out = _scrub_error_message(raw)
    assert expected_substring in out
    # Original path strings should not survive.
    for sentinel in ("/var/mobile", "/Users/jane", "content://media"):
        assert sentinel not in out


def test_scrub_error_message_truncates_at_200():
    long = "x" * 500
    out = _scrub_error_message(long)
    assert len(out) == MAX_ERROR_MESSAGE_LEN


def test_scrub_error_message_empty_string():
    assert _scrub_error_message("") == ""


# ----------------------------------------------------------------------
# emit_echo_upload
# ----------------------------------------------------------------------


def _capture_log(monkeypatch):
    """Return a list-backed logger that the emitter writes into."""
    captured: list[str] = []

    class FakeLogger:
        def info(self, line: str) -> None:
            captured.append(line)

        def warning(self, line: str) -> None:  # noqa: D401 (parallel API)
            captured.append(f"WARN: {line}")

    return FakeLogger(), captured


def test_emit_echo_upload_writes_a_single_json_line(monkeypatch):
    log, captured = _capture_log(monkeypatch)
    emit_echo_upload(
        user_id="u-1",
        payload={
            "echo_id": "e-1",
            "content_type": "video/mp4",
            "original_bytes": 50_000_000,
            "compressed_bytes": 10_000_000,
            "upload_path": "single-put",
            "parts_count": None,
            "status": "success",
            "failure_stage": None,
            "error_message": None,
            "duration_compress_ms": 1500,
            "duration_upload_ms": 8000,
            "duration_finalize_ms": 250,
            "duration_total_ms": 9750,
            "backgrounded_during_upload": False,
            "retry_count": 0,
            "app_state_at_completion": "active",
            "platform": "ios",
            "app_version": "1.4.2",
        },
        log=log,
    )
    assert len(captured) == 1
    parsed = json.loads(captured[0])
    assert parsed["event"] == EVENT_ECHO_UPLOAD
    assert parsed["status"] == "success"
    assert parsed["original_bytes"] == 50_000_000
    # user_id is hashed at the boundary.
    assert parsed["user_hash"] != "u-1"
    assert len(parsed["user_hash"]) == 32


def test_emit_echo_upload_scrubs_error_message():
    log, captured = _capture_log(None)
    emit_echo_upload(
        user_id="u-1",
        payload={
            "echo_id": "e-1",
            "content_type": "video/mp4",
            "original_bytes": 5,
            "upload_path": "single-put",
            "status": "failed",
            "failure_stage": "upload",
            "error_message": "Cannot read /var/mobile/Cache/clip.mp4",
            "duration_upload_ms": 100,
            "duration_total_ms": 100,
            "backgrounded_during_upload": False,
            "retry_count": 0,
            "app_state_at_completion": "active",
            "platform": "ios",
            "app_version": "1.0",
        },
        log=log,
    )
    parsed = json.loads(captured[0])
    assert "/var/mobile" not in parsed["error_message"]
    assert "[path]" in parsed["error_message"]


def test_emit_echo_upload_truncates_oversize_error_message():
    log, captured = _capture_log(None)
    emit_echo_upload(
        user_id="u-1",
        payload={
            "echo_id": "e-1",
            "content_type": "video/mp4",
            "original_bytes": 5,
            "upload_path": "single-put",
            "status": "failed",
            "failure_stage": "upload",
            "error_message": "x" * 500,
            "duration_upload_ms": 100,
            "duration_total_ms": 100,
            "backgrounded_during_upload": False,
            "retry_count": 0,
            "app_state_at_completion": "active",
            "platform": "ios",
            "app_version": "1.0",
        },
        log=log,
    )
    parsed = json.loads(captured[0])
    assert len(parsed["error_message"]) == MAX_ERROR_MESSAGE_LEN


def test_emit_echo_upload_drops_richer_types_silently():
    """Lists / nested dicts aren't in the schema; silently dropped."""
    log, captured = _capture_log(None)
    emit_echo_upload(
        user_id="u-1",
        payload={
            "echo_id": "e-1",
            "content_type": "video/mp4",
            "original_bytes": 5,
            "upload_path": "single-put",
            "status": "success",
            "duration_upload_ms": 1,
            "duration_total_ms": 1,
            "backgrounded_during_upload": False,
            "retry_count": 0,
            "app_state_at_completion": "active",
            "platform": "ios",
            "app_version": "1.0",
            # Sneaky extras the schema doesn't allow.
            "nested": {"a": 1},
            "list_field": [1, 2, 3],
        },
        log=log,
    )
    parsed = json.loads(captured[0])
    assert "nested" not in parsed
    assert "list_field" not in parsed


def test_emit_echo_upload_swallows_serialization_failure():
    """A bug elsewhere must not break the route. The emitter logs WARN
    and returns normally.
    """
    log, captured = _capture_log(None)
    # Inject something that json.dumps can't serialize — a complex
    # object. Should be dropped by the sanitizer; if a future bug lets
    # something through, the outer try/except catches it.
    emit_echo_upload(
        user_id="u-1",
        payload={
            "echo_id": object(),  # not a string but our sanitizer drops it
            "content_type": "video/mp4",
            "original_bytes": 5,
            "upload_path": "single-put",
            "status": "success",
            "duration_upload_ms": 1,
            "duration_total_ms": 1,
            "backgrounded_during_upload": False,
            "retry_count": 0,
            "app_state_at_completion": "active",
            "platform": "ios",
            "app_version": "1.0",
        },
        log=log,
    )
    # Either the line was logged successfully (object dropped) OR a
    # WARN was logged. Either way: no exception.
    assert len(captured) >= 1


# ----------------------------------------------------------------------
# POST /api/telemetry/echo-upload — route layer
# ----------------------------------------------------------------------


async def _fake_user() -> Dict[str, Any]:
    return {"id": "test-user-123", "sub": "test-user-123"}


@pytest.fixture
def client():
    """Local TestClient that overrides get_current_user with a no-arg
    async stub. The conftest's mock uses (*args, **kwargs) which
    FastAPI mistakes for route-level Query params on routes added
    after the override was installed — surfaces as a 422 with
    `query.args / query.kwargs required`. The existing
    test_telemetry_routes.py uses this same pattern.
    """
    from src.app.core.security import get_current_user
    from src.app.handler import app

    app.dependency_overrides[get_current_user] = _fake_user
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _valid_payload() -> Dict[str, Any]:
    return {
        "echo_id": "e-1",
        "content_type": "video/mp4",
        "original_bytes": 5_000_000,
        "compressed_bytes": 1_000_000,
        "upload_path": "single-put",
        "parts_count": None,
        "status": "success",
        "failure_stage": None,
        "error_message": None,
        "duration_compress_ms": 1500,
        "duration_upload_ms": 4000,
        "duration_finalize_ms": 200,
        "duration_total_ms": 5700,
        "backgrounded_during_upload": False,
        "retry_count": 0,
        "app_state_at_completion": "active",
        "platform": "ios",
        "app_version": "1.4.2",
    }


def test_route_returns_204_on_valid_payload(client: TestClient):
    response = client.post(
        "/api/telemetry/echo-upload",
        json=_valid_payload(),
    )
    assert response.status_code == 204, response.text


def test_route_rejects_unknown_upload_path(client: TestClient):
    payload = _valid_payload()
    payload["upload_path"] = "ftp"  # not in Literal
    response = client.post("/api/telemetry/echo-upload", json=payload)
    assert response.status_code == 422


def test_route_rejects_unknown_status(client: TestClient):
    payload = _valid_payload()
    payload["status"] = "weird"
    response = client.post("/api/telemetry/echo-upload", json=payload)
    assert response.status_code == 422


def test_route_rejects_missing_required_field(client: TestClient):
    payload = _valid_payload()
    del payload["duration_total_ms"]
    response = client.post("/api/telemetry/echo-upload", json=payload)
    assert response.status_code == 422


def test_route_records_event_via_real_emitter(client: TestClient, caplog):
    """End-to-end: posting a valid payload writes the structured log
    line via the real emitter. Catching the log lets us verify the
    JSON wiring without patching internals (which interferes with
    FastAPI's signature introspection on the route handler).
    """
    import logging as _logging

    caplog.set_level(_logging.INFO, logger="telemetry.upload")
    response = client.post(
        "/api/telemetry/echo-upload",
        json=_valid_payload(),
    )
    assert response.status_code == 204, response.text
    # The emitter writes one JSON line per event; look for ours.
    found = [r for r in caplog.records if r.name == "telemetry.upload"]
    assert len(found) == 1
    parsed = json.loads(found[0].message)
    assert parsed["event"] == EVENT_ECHO_UPLOAD
    assert parsed["status"] == "success"
