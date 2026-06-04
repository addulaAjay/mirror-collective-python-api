"""
Tests for the rich echo-share emails (Voice / Video / Written).

EmailService.send_echo_share_email renders the compiled MJML templates in
emails/dist/*.html with Jinja2 and sends them via SES. These tests exercise
the render path against the *real* committed templates (so a broken template
or a renamed placeholder fails CI), plus the security-critical autoescape and
the render-failure fallback.
"""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

# Point the template loader at the committed compiled templates regardless of
# CWD, before EmailService imports resolve the dir.
_DIST = Path(__file__).resolve().parents[1] / "emails" / "dist"
os.environ.setdefault("EMAIL_TEMPLATE_DIR", str(_DIST))
os.environ.setdefault("EMAIL_ASSET_BASE_URL", "https://cdn.test/email-assets")

from src.app.services.email_service import (  # noqa: E402
    EmailService,
    _format_echo_date,
    _split_paragraphs,
)


def _service_with_capture():
    """EmailService whose _send_email captures args instead of hitting SES."""
    service = EmailService()
    captured = {}

    async def fake_send(to_email, subject, html_body, text_body):
        captured[to_email] = {
            "subject": subject,
            "html": html_body,
            "text": text_body,
        }
        return True

    service._send_email = fake_send  # type: ignore[assignment]
    return service, captured


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_format_echo_date_ordinals():
    assert _format_echo_date("2025-05-04T10:00:00Z") == "May 4th, 2025"
    assert _format_echo_date("2025-05-01T00:00:00Z") == "May 1st, 2025"
    assert _format_echo_date("2025-05-02T00:00:00Z") == "May 2nd, 2025"
    assert _format_echo_date("2025-05-03T00:00:00Z") == "May 3rd, 2025"
    assert _format_echo_date("2025-05-11T00:00:00Z") == "May 11th, 2025"
    assert _format_echo_date("2025-05-21T00:00:00Z") == "May 21st, 2025"


def test_format_echo_date_handles_bad_input():
    # Garbage / None must not raise; falls back to "now".
    assert ", " in _format_echo_date(None)
    assert ", " in _format_echo_date("not-a-date")


def test_split_paragraphs():
    assert _split_paragraphs("a\n\nb") == ["a", "b"]
    assert _split_paragraphs("only one") == ["only one"]
    assert _split_paragraphs("") == [""]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "echo_type,label",
    [("AUDIO", "Voice"), ("VIDEO", "Video"), ("TEXT", "Written")],
)
async def test_renders_each_type(echo_type, label):
    service, captured = _service_with_capture()

    ok = await service.send_echo_share_email(
        recipient_email="r@example.com",
        sender_name="Jane Smith",
        echo_type=echo_type,
        quote="A gentle message.",
        echo_date="2025-05-04T10:00:00Z",
        open_echo_url="https://app.test/echoes/123",
    )

    assert ok is True
    out = captured["r@example.com"]
    html = out["html"]
    # Subject + no unrendered Jinja left behind.
    assert out["subject"] == f"Your {label} Echo from Jane Smith"
    assert "{{" not in html and "{%" not in html
    # Personalization + asset base resolved.
    assert "Jane Smith" in html
    assert "May 4th, 2025" in html
    assert "https://cdn.test/email-assets/logo-mirror-collective.png" in html
    # CTA + privacy footer always present.
    assert "GET THE APP" in html
    assert "Echo Vault" in html
    # Plain-text alternative is non-trivial.
    assert "Jane Smith" in out["text"] and len(out["text"]) > 80


@pytest.mark.asyncio
async def test_quote_is_autoescaped():
    """Sender-authored quote must be HTML-escaped (injection guard)."""
    service, captured = _service_with_capture()

    await service.send_echo_share_email(
        recipient_email="r@example.com",
        sender_name="Jane",
        echo_type="AUDIO",
        quote="<script>alert(1)</script> & friends",
    )

    html = captured["r@example.com"]["html"]
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html and "&amp; friends" in html


@pytest.mark.asyncio
async def test_written_renders_multiple_paragraphs():
    service, captured = _service_with_capture()

    await service.send_echo_share_email(
        recipient_email="r@example.com",
        sender_name="Jane",
        echo_type="TEXT",
        quote="First paragraph.\n\nSecond paragraph.",
    )

    html = captured["r@example.com"]["html"]
    assert "First paragraph." in html
    assert "Second paragraph." in html


@pytest.mark.asyncio
async def test_audio_includes_duration():
    service, captured = _service_with_capture()

    await service.send_echo_share_email(
        recipient_email="r@example.com",
        sender_name="Jane",
        echo_type="AUDIO",
        quote="hi",
        media_duration="2:32",
    )

    assert "2:32" in captured["r@example.com"]["html"]


@pytest.mark.asyncio
async def test_render_failure_falls_back_to_inline_html():
    """A missing/broken template must not drop the notification."""
    service, captured = _service_with_capture()

    with patch("src.app.services.email_service._template_env") as mock_env:
        mock_env.return_value.get_template.side_effect = OSError("missing dist")
        ok = await service.send_echo_share_email(
            recipient_email="r@example.com",
            sender_name="Jane",
            echo_type="AUDIO",
            quote="hi",
        )

    assert ok is True
    html = captured["r@example.com"]["html"]
    assert "Your Voice Echo" in html
    assert "GET THE APP" in html
    assert "{{" not in html


@pytest.mark.asyncio
async def test_send_echo_notification_registered_uses_rich_template():
    """The registered-recipient path delegates to the rich renderer."""
    service, captured = _service_with_capture()

    await service.send_echo_notification(
        recipient_email="r@example.com",
        recipient_name="Recipient",
        sender_name="Jane Smith",
        echo_title="A Title",
        echo_category="Memory",
        echo_type="VIDEO",
        is_registered=True,
        echo_date="2025-05-04T10:00:00Z",
    )

    out = captured["r@example.com"]
    assert out["subject"] == "Your Video Echo from Jane Smith"
    assert "Your Video Echo" in out["html"]
