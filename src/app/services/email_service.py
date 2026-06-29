"""
Email Service for Echo Vault notifications.

Echo-only email policy: the service sends mail only for echo delivery. Every
recipient — registered or not — receives the same rich, Figma-designed
echo-share email.

No other mail is sent: recipient invites, guardian invites, guardian "echo
pending" notifications, and the unregistered-recipient "download the app"
invitation have all been removed.
"""

import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError
from jinja2 import Environment, FileSystemLoader, TemplateError, select_autoescape

from ..core.log_sanitize import mask_email

logger = logging.getLogger(__name__)


# Shared botocore Config for SES clients. We open a fresh aioboto3 client per
# send (cheap with aioboto3), but every client honours these tuned defaults so
# burst email traffic doesn't saturate the default 10-connection pool or fall
# off the cliff during SES throttling events.
_SES_CLIENT_CONFIG = Config(
    max_pool_connections=50,
    retries={"max_attempts": 5, "mode": "adaptive"},
)


# ---------------------------------------------------------------------------
# Echo-share email (Figma 5535:5144 -> MJML -> compiled HTML).
#
# ONE generic template for every echo type. The email is a link, not a player:
# it shows who shared an echo plus a short cover note, then sends the recipient
# into the app (open_echo_url), which presents the full echo. Source lives in
# emails/src/echo.mjml; `npm run build` compiles it to emails/dist/echo.html
# (committed + packaged with the Lambda — see serverless.yml `package.patterns`
# and emails/README.md). At render time the compiled HTML is a Jinja2 template;
# we fill the {{ }} placeholders here.
# ---------------------------------------------------------------------------
_ECHO_TEMPLATE = "echo.html"

# Boilerplate used only when the sender left no cover note / message of their
# own. Mirrors the sample copy in the Figma frame, kept generic across types.
_DEFAULT_QUOTE = (
    "A private moment of presence, memory, and meaning has been shared with "
    "you. Open your echo to experience it as it was meant to be."
)

_DAY_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def _template_dir() -> Path:
    """Directory holding the compiled email templates.

    Overridable via EMAIL_TEMPLATE_DIR (tests / non-standard layouts). Default
    resolves from this file: services -> app -> src -> <repo root> -> emails/dist.
    """
    override = os.getenv("EMAIL_TEMPLATE_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "emails" / "dist"


@lru_cache(maxsize=1)
def _template_env() -> Environment:
    """Lazily build the Jinja2 environment over the compiled templates.

    autoescape is ON: quote text is sender-authored and must be escaped to
    prevent HTML injection into the email body.
    """
    return Environment(
        loader=FileSystemLoader(str(_template_dir())),
        autoescape=select_autoescape(["html", "xml"]),
    )


def _format_echo_date(value: Optional[str]) -> str:
    """Format an ISO date/datetime string (or None=now) as 'May 4th, 2025'."""
    dt: Optional[datetime] = None
    if value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)
    day = dt.day
    suffix = "th" if 11 <= (day % 100) <= 13 else _DAY_SUFFIXES.get(day % 10, "th")
    return f"{dt.strftime('%B')} {day}{suffix}, {dt.year}"


def _split_paragraphs(text: str) -> List[str]:
    """Split a message into paragraphs on blank lines; never returns empty."""
    parts = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    return parts or [(text or "").strip()]


class EmailService:
    """
    Service for sending Echo Vault notification emails via AWS SES.
    """

    def __init__(self):
        """Initialize Email service"""
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.sender_email = os.getenv("SES_SENDER_EMAIL", "app@themirrorcollective.com")
        self.app_name = os.getenv("APP_NAME", "Mirror Collective")
        self.app_url = os.getenv("APP_URL", "https://mirrorcollective.com")
        self.app_store_url = os.getenv(
            "APP_STORE_URL", "https://apps.apple.com/app/mirror-collective"
        )
        self.play_store_url = os.getenv(
            "PLAY_STORE_URL",
            "https://play.google.com/store/apps/details?id=com.mirrorcollective",
        )
        # CDN base for static brand assets referenced by the rich echo templates
        # (logo, gold play button, lock, download, waveform, star divider).
        self.asset_base = os.getenv(
            "EMAIL_ASSET_BASE_URL", f"{self.app_url}/email-assets"
        )

        # Initialize aioboto3 session
        self.session = aioboto3.Session()

        logger.info(f"EmailService initialized - Sender: {self.sender_email}")

    async def send_echo_notification(
        self,
        recipient_email: str,
        recipient_name: str,
        sender_name: str,
        echo_title: str,
        echo_category: str,
        echo_type: str,
        is_registered: bool = True,
        *,
        quote: Optional[str] = None,
        echo_date: Optional[str] = None,
        open_echo_url: Optional[str] = None,
        hero_image_url: Optional[str] = None,
        media_duration: Optional[str] = None,
        attachment_count: int = 0,
        attachment_url: Optional[str] = None,
        attachment_thumb_url: Optional[str] = None,
        media_blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """
        Send notification when an echo is released to a recipient.

        Every recipient — registered or not — receives the rich, Figma-designed
        echo-share email (delegated to :meth:`send_echo_share_email`). No
        invitation / "download the app" email is sent (echo-only email policy).

        The keyword-only args enrich the rich template; callers that have the
        full Echo (e.g. echo_service) should pass them. They are optional so
        existing callers keep working — missing fields degrade gracefully
        (boilerplate quote, default hero image, no attachment row).

        Args:
            recipient_email: Email address of the recipient
            recipient_name: Name of the recipient
            sender_name: Name who created the echo
            echo_title: Title of the echo
            echo_category: Category of the echo
            echo_type: Type of echo (TEXT, AUDIO, VIDEO)
            is_registered: Whether recipient has an account (default True)
            quote: Sender's cover note / message; falls back to per-type copy
            echo_date: ISO date of release; formatted to "May 4th, 2025"
            open_echo_url: Deep link / web URL the email's CTA + media point to
            hero_image_url: Hero/poster image; falls back to a default asset
            media_duration: Audio/video length, e.g. "2:32" (voice/video only)
            attachment_count: Number of downloadable attachments (0 = no row)
            attachment_url: Download URL for attachments
            attachment_thumb_url: Thumbnail for the attachment row

        Returns:
            True if email was sent successfully
        """
        # Echo-only email policy: every recipient — registered or not — gets
        # the echo-share email. No invitation / "download the app" email is
        # sent. ``is_registered`` is retained for caller back-compat only.
        return await self.send_echo_share_email(
            recipient_email=recipient_email,
            sender_name=sender_name,
            echo_type=echo_type,
            quote=quote or _DEFAULT_QUOTE,
            echo_date=echo_date,
            open_echo_url=open_echo_url,
            hero_image_url=hero_image_url,
            media_duration=media_duration,
            attachment_count=attachment_count,
            attachment_url=attachment_url,
            attachment_thumb_url=attachment_thumb_url,
            media_blocks=media_blocks,
        )

    async def send_echo_share_email(
        self,
        *,
        recipient_email: str,
        sender_name: str,
        echo_type: str = "",
        quote: str,
        echo_date: Optional[str] = None,
        open_echo_url: Optional[str] = None,
        # Back-compat: callers spread build_email_media_fields() in via
        # **media_fields. The generic template is a link — it doesn't embed
        # media — so these are accepted and ignored (open_echo_url is the one
        # that matters; it deep-links into the app where the full echo lives).
        hero_image_url: Optional[str] = None,
        media_duration: Optional[str] = None,
        attachment_count: int = 0,
        attachment_url: Optional[str] = None,
        attachment_thumb_url: Optional[str] = None,
        media_blocks: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """
        Render and send the generic echo-share email (one template, all types).

        Renders the compiled MJML template (emails/dist/echo.html) with Jinja2
        and sends it via SES with a plain-text alternative. On any template/
        render failure, falls back to a minimal inline HTML so the notification
        still goes out.

        ``echo_type`` no longer selects a template (kept only for caller
        back-compat). The email links to ``open_echo_url``; the app presents the
        full echo — media, attachments, everything.

        Returns:
            True if email was sent successfully
        """
        open_url = open_echo_url or self.app_url
        formatted_date = _format_echo_date(echo_date)
        subject = f"Your Echo from {sender_name}"

        context: dict = {
            "app_name": self.app_name,
            "asset_base": self.asset_base,
            "sender_name": sender_name,
            "echo_date": formatted_date,
            "open_echo_url": open_url,
            "quote_text": quote,
        }

        text_body = self._echo_share_text(
            sender_name=sender_name,
            echo_date=formatted_date,
            quote=quote,
            open_url=open_url,
        )

        try:
            template = _template_env().get_template(_ECHO_TEMPLATE)
            html_body = template.render(**context)
        except (TemplateError, OSError) as e:
            logger.error(
                f"Echo template render failed ({_ECHO_TEMPLATE}): {e}. "
                "Falling back to plain notification."
            )
            html_body = self._echo_share_fallback_html(
                sender_name=sender_name,
                echo_date=formatted_date,
                quote=quote,
                open_url=open_url,
            )

        return await self._send_email(
            to_email=recipient_email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )

    def _echo_share_text(
        self,
        *,
        sender_name: str,
        echo_date: str,
        quote: str,
        open_url: str,
    ) -> str:
        """Plain-text alternative for the echo email (required by SES)."""
        return f"""Your Echo

A private message has been shared with you
from {sender_name} on {echo_date}.

Click here to open your echo: {open_url}

"{quote}"

---
Shared privately through Echo Vault.
This is an automated message from {self.app_name}.
If you didn't expect this email, you can safely ignore it.
"""

    def _echo_share_fallback_html(
        self,
        *,
        sender_name: str,
        echo_date: str,
        quote: str,
        open_url: str,
    ) -> str:
        """Minimal inline-styled HTML used only if template rendering fails."""
        safe_quote = (
            (quote or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return f"""<!DOCTYPE html>
<html>
<body style="margin:0;background:#0b1020;color:#fdfdf9;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:600px;margin:0 auto;padding:40px 24px;background:#0b1020;">
    <h1 style="color:#f2e1b0;font-family:Georgia,serif;text-align:center;">Your Echo</h1>
    <p style="color:#a3b3cc;text-align:center;">
      A private message has been shared with you from
      {sender_name} on {echo_date}.
    </p>
    <p style="text-align:center;font-style:italic;">
      <a href="{open_url}" style="color:#f2e1b0;text-decoration:none;">Click here to open your echo.</a>
    </p>
    <p style="background:#131a2e;border:1px solid #f0d4a8;border-radius:12px;padding:24px;text-align:center;">
      &ldquo;{safe_quote}&rdquo;
    </p>
    <p style="text-align:center;">
      <a href="{open_url}" style="display:inline-block;background:#0b1020;color:#f2e1b0;
         border:1px solid #a3b3cc;padding:12px 24px;border-radius:12px;text-decoration:none;">GET THE APP</a>
    </p>
    <p style="color:#a3b3cc;font-size:12px;text-align:center;">
      Shared privately through Echo Vault. This is an automated message from {self.app_name}.
    </p>
  </div>
</body>
</html>"""

    async def _send_email(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        text_body: str,
    ) -> bool:
        """
        Send email via AWS SES.

        Args:
            to_email: Recipient email address
            subject: Email subject
            html_body: HTML content
            text_body: Plain text content

        Returns:
            True if sent successfully
        """
        try:
            async with self.session.client(
                "ses", region_name=self.region, config=_SES_CLIENT_CONFIG
            ) as ses:
                response = await ses.send_email(
                    Source=self.sender_email,
                    Destination={
                        "ToAddresses": [to_email],
                    },
                    Message={
                        "Subject": {
                            "Data": subject,
                            "Charset": "UTF-8",
                        },
                        "Body": {
                            "Text": {
                                "Data": text_body,
                                "Charset": "UTF-8",
                            },
                            "Html": {
                                "Data": html_body,
                                "Charset": "UTF-8",
                            },
                        },
                    },
                )

                message_id = response.get("MessageId", "unknown")
                logger.info(
                    f"Email sent to {mask_email(to_email)}, MessageId: {message_id}"
                )
                return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error(
                f"SES error sending email to {mask_email(to_email)}: {error_code}"
            )
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error sending email to {mask_email(to_email)}: {e}"
            )
            return False


# Singleton instance
email_service = EmailService()
