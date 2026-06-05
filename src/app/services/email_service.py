"""
Email Service for Echo Vault notifications.
Uses AWS SES to send emails for:
- Recipient invitations
- Guardian invitations
- Echo release notifications
"""

import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError
from jinja2 import Environment, FileSystemLoader, TemplateError, select_autoescape

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
# Rich Echo-share templates (Figma "Email Templates" -> MJML -> compiled HTML).
#
# Sources live in emails/src/*.mjml; `npm run build` compiles them to
# emails/dist/*.html (committed + packaged with the Lambda — see
# serverless.yml `package.patterns` and emails/README.md). At render time the
# compiled HTML is a Jinja2 template; we fill the {{ }} placeholders here.
# ---------------------------------------------------------------------------
_ECHO_TEMPLATE_BY_TYPE = {
    "TEXT": "echo-written.html",
    "AUDIO": "echo-voice.html",
    "VIDEO": "echo-video.html",
}

_ECHO_TYPE_LABEL = {"TEXT": "Written", "AUDIO": "Voice", "VIDEO": "Video"}

# Boilerplate used only when the sender left no cover note / message of their
# own. Mirrors the sample copy in the Figma frames.
_DEFAULT_QUOTE_BY_TYPE = {
    "AUDIO": (
        "Some things are meant to be heard, not just read. This voice echo was "
        "shared with you as a private moment of presence, memory, and meaning."
    ),
    "VIDEO": (
        "When words needed more presence, this message was recorded for you. "
        "Open the echo to watch the full video and experience it as intended."
    ),
    "TEXT": "A private message has been shared with you through Echo Vault.",
}

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

    async def send_recipient_invite(
        self,
        recipient_email: str,
        recipient_name: str,
        inviter_name: str,
    ) -> bool:
        """
        Send invitation email to a new recipient.

        Args:
            recipient_email: Email address of the recipient
            recipient_name: Name of the recipient
            inviter_name: Name of the person adding the recipient

        Returns:
            True if email was sent successfully
        """
        subject = f"{inviter_name} has added you as a trusted recipient"

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: 'Inter', Arial, sans-serif; background: #1a1a2e; color: #fdfdf9; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 40px 20px; }}
                .header {{ text-align: center; margin-bottom: 40px; }}
                .logo {{ color: #f2e2b1; font-size: 28px; font-family: 'Cormorant Garamond', serif; }}
                .content {{ background: rgba(255,255,255,0.05); border-radius: 12px; padding: 30px; }}
                .highlight {{ color: #f2e2b1; }}
                .footer {{ text-align: center; margin-top: 40px; color: #a3b3cc; font-size: 12px; }}
                .button {{ display: inline-block; background: linear-gradient(135deg, #f2e2b1, #d4c79e);
                          color: #1a1a2e; padding: 14px 28px; border-radius: 8px;
                          text-decoration: none; font-weight: 600; margin-top: 20px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="logo">Mirror Collective</div>
                </div>
                <div class="content">
                    <p>Hello <span class="highlight">{recipient_name}</span>,</p>
                    <p><strong>{inviter_name}</strong> has added you as a trusted recipient
                    in their Echo Vault on {self.app_name}.</p>
                    <p>As a recipient, you may receive meaningful messages, memories, or
                    reflections that {inviter_name} wants to share with you at special moments.</p>
                    <p>When an echo is released to you, you'll receive another notification.</p>
                    <a href="{self.app_url}" class="button">Learn More</a>
                </div>
                <div class="footer">
                    <p>This is an automated message from {self.app_name}.</p>
                    <p>If you didn't expect this email, you can safely ignore it.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_body = f"""
Hello {recipient_name},

{inviter_name} has added you as a trusted recipient in their Echo Vault on {self.app_name}.

As a recipient, you may receive meaningful messages, memories, or reflections that
{inviter_name} wants to share with you at special moments.

When an echo is released to you, you'll receive another notification.

Learn more: {self.app_url}

---
This is an automated message from {self.app_name}.
If you didn't expect this email, you can safely ignore it.
        """

        return await self._send_email(
            to_email=recipient_email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )

    async def send_guardian_invite(
        self,
        guardian_email: str,
        guardian_name: str,
        inviter_name: str,
        scope: str = "ALL",
    ) -> bool:
        """
        Send invitation email to a new guardian.

        Args:
            guardian_email: Email address of the guardian
            guardian_name: Name of the guardian
            inviter_name: Name of the person adding the guardian
            scope: Access scope (ALL or SELECTED)

        Returns:
            True if email was sent successfully
        """
        subject = f"{inviter_name} has named you as an Echo Guardian"

        scope_text = "all echoes" if scope == "ALL" else "selected echoes"

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: 'Inter', Arial, sans-serif; background: #1a1a2e; color: #fdfdf9; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 40px 20px; }}
                .header {{ text-align: center; margin-bottom: 40px; }}
                .logo {{ color: #f2e2b1; font-size: 28px; font-family: 'Cormorant Garamond', serif; }}
                .content {{ background: rgba(255,255,255,0.05); border-radius: 12px; padding: 30px; }}
                .highlight {{ color: #f2e2b1; }}
                .footer {{ text-align: center; margin-top: 40px; color: #a3b3cc; font-size: 12px; }}
                .button {{ display: inline-block; background: linear-gradient(135deg, #f2e2b1, #d4c79e);
                          color: #1a1a2e; padding: 14px 28px; border-radius: 8px;
                          text-decoration: none; font-weight: 600; margin-top: 20px; }}
                .info-box {{ background: rgba(242,226,177,0.1); border-left: 3px solid #f2e2b1;
                            padding: 15px; margin: 20px 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="logo">Mirror Collective</div>
                </div>
                <div class="content">
                    <p>Hello <span class="highlight">{guardian_name}</span>,</p>
                    <p><strong>{inviter_name}</strong> has named you as an <strong>Echo Guardian</strong>
                    on {self.app_name}.</p>

                    <div class="info-box">
                        <p><strong>What is an Echo Guardian?</strong></p>
                        <p>As a guardian, you are entrusted with managing the release of
                        {inviter_name}'s echoes—meaningful messages, memories, and reflections
                        they want to share with their loved ones.</p>
                    </div>

                    <p>Your access includes: <span class="highlight">{scope_text}</span></p>

                    <p>This is a meaningful responsibility, and {inviter_name} trusts you
                    to handle it with care when the time comes.</p>

                    <a href="{self.app_url}" class="button">Learn More</a>
                </div>
                <div class="footer">
                    <p>This is an automated message from {self.app_name}.</p>
                    <p>If you didn't expect this email, please contact us.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_body = f"""
Hello {guardian_name},

{inviter_name} has named you as an Echo Guardian on {self.app_name}.

WHAT IS AN ECHO GUARDIAN?
As a guardian, you are entrusted with managing the release of {inviter_name}'s
echoes—meaningful messages, memories, and reflections they want to share with
their loved ones.

Your access includes: {scope_text}

This is a meaningful responsibility, and {inviter_name} trusts you to handle it
with care when the time comes.

Learn more: {self.app_url}

---
This is an automated message from {self.app_name}.
If you didn't expect this email, please contact us.
        """

        return await self._send_email(
            to_email=guardian_email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )

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
    ) -> bool:
        """
        Send notification when an echo is released to a recipient.

        Registered recipients get the rich, Figma-designed echo email
        (delegated to :meth:`send_echo_share_email`). Unregistered recipients
        get the download-the-app invitation instead.

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
        if not is_registered:
            # Send invitation email encouraging them to register
            return await self._send_echo_invitation_to_register(
                recipient_email=recipient_email,
                recipient_name=recipient_name,
                sender_name=sender_name,
                echo_title=echo_title,
                echo_category=echo_category,
                echo_type=echo_type,
            )

        # Registered recipients get the rich, branded echo email.
        return await self.send_echo_share_email(
            recipient_email=recipient_email,
            sender_name=sender_name,
            echo_type=echo_type,
            quote=quote
            or _DEFAULT_QUOTE_BY_TYPE.get(
                (echo_type or "TEXT").upper(), _DEFAULT_QUOTE_BY_TYPE["TEXT"]
            ),
            echo_date=echo_date,
            open_echo_url=open_echo_url,
            hero_image_url=hero_image_url,
            media_duration=media_duration,
            attachment_count=attachment_count,
            attachment_url=attachment_url,
            attachment_thumb_url=attachment_thumb_url,
        )

    async def send_echo_share_email(
        self,
        *,
        recipient_email: str,
        sender_name: str,
        echo_type: str,
        quote: str,
        echo_date: Optional[str] = None,
        open_echo_url: Optional[str] = None,
        hero_image_url: Optional[str] = None,
        media_duration: Optional[str] = None,
        attachment_count: int = 0,
        attachment_url: Optional[str] = None,
        attachment_thumb_url: Optional[str] = None,
    ) -> bool:
        """
        Render and send the rich echo-share email (Voice / Video / Written).

        Renders the compiled MJML template for ``echo_type`` with Jinja2 and
        sends it via SES with a plain-text alternative. On any template/render
        failure, falls back to a minimal inline HTML so the notification still
        goes out.

        Returns:
            True if email was sent successfully
        """
        echo_type = (echo_type or "TEXT").upper()
        template_name = _ECHO_TEMPLATE_BY_TYPE.get(echo_type, "echo-written.html")
        type_label = _ECHO_TYPE_LABEL.get(echo_type, "Written")
        open_url = open_echo_url or self.app_url
        formatted_date = _format_echo_date(echo_date)
        subject = f"Your {type_label} Echo from {sender_name}"

        context: dict = {
            "app_name": self.app_name,
            "asset_base": self.asset_base,
            "sender_name": sender_name,
            "echo_date": formatted_date,
            "open_echo_url": open_url,
            "hero_image_url": hero_image_url or f"{self.asset_base}/hero-default.jpg",
            "attachment_count": attachment_count or 0,
            "attachment_url": attachment_url or open_url,
            "attachment_thumb_url": attachment_thumb_url
            or f"{self.asset_base}/attachment-thumb.png",
            "audio_duration": media_duration or "",
            "video_duration": media_duration or "",
        }
        # Written renders multi-paragraph; voice/video use a single block.
        if echo_type == "TEXT":
            context["quote_paragraphs"] = _split_paragraphs(quote)
        else:
            context["quote_text"] = quote

        text_body = self._echo_share_text(
            type_label=type_label,
            sender_name=sender_name,
            echo_date=formatted_date,
            quote=quote,
            open_url=open_url,
        )

        try:
            template = _template_env().get_template(template_name)
            html_body = template.render(**context)
        except (TemplateError, OSError) as e:
            logger.error(
                f"Echo template render failed ({template_name}): {e}. "
                "Falling back to plain notification."
            )
            html_body = self._echo_share_fallback_html(
                type_label=type_label,
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
        type_label: str,
        sender_name: str,
        echo_date: str,
        quote: str,
        open_url: str,
    ) -> str:
        """Plain-text alternative for the rich echo email (required by SES)."""
        return f"""Your {type_label} Echo

A private {type_label.lower()} message has been shared with you
from {sender_name} on {echo_date}.

"{quote}"

Open your echo: {open_url}

---
Shared privately through Echo Vault.
This is an automated message from {self.app_name}.
If you didn't expect this email, you can safely ignore it.
"""

    def _echo_share_fallback_html(
        self,
        *,
        type_label: str,
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
    <h1 style="color:#f2e1b0;font-family:Georgia,serif;text-align:center;">Your {type_label} Echo</h1>
    <p style="color:#a3b3cc;text-align:center;">
      A private {type_label.lower()} message has been shared with you from
      {sender_name} on {echo_date}.
    </p>
    <p style="background:#131a2e;border:1px solid #2a3450;border-radius:12px;padding:24px;text-align:center;">
      &ldquo;{safe_quote}&rdquo;
    </p>
    <p style="text-align:center;">
      <a href="{open_url}" style="display:inline-block;background:#f2e1b0;color:#0b1020;
         padding:14px 36px;border-radius:8px;text-decoration:none;font-weight:600;">GET THE APP</a>
    </p>
    <p style="color:#a3b3cc;font-size:12px;text-align:center;">
      Shared privately through Echo Vault. This is an automated message from {self.app_name}.
    </p>
  </div>
</body>
</html>"""

    async def _send_echo_invitation_to_register(
        self,
        recipient_email: str,
        recipient_name: str,
        sender_name: str,
        echo_title: str,
        echo_category: str,
        echo_type: str,
    ) -> bool:
        """
        Send invitation email to non-registered recipient encouraging them to download app.

        Args:
            recipient_email: Email address of the recipient
            recipient_name: Name of the recipient
            sender_name: Name who created the echo
            echo_title: Title of the echo
            echo_category: Category of the echo
            echo_type: Type of echo (TEXT, AUDIO, VIDEO)

        Returns:
            True if email was sent successfully
        """
        subject = f"💫 {sender_name} sent you a personal message on {self.app_name}"

        type_icon = {"TEXT": "📝", "AUDIO": "🎤", "VIDEO": "🎬"}.get(echo_type, "✨")

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: 'Inter', Arial, sans-serif; background: #1a1a2e; color: #fdfdf9; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 40px 20px; }}
                .header {{ text-align: center; margin-bottom: 40px; }}
                .logo {{ color: #f2e2b1; font-size: 28px; font-family: 'Cormorant Garamond', serif; }}
                .content {{ background: rgba(255,255,255,0.05); border-radius: 12px; padding: 30px; }}
                .highlight {{ color: #f2e2b1; }}
                .footer {{ text-align: center; margin-top: 40px; color: #a3b3cc; font-size: 12px; }}
                .cta-section {{ text-align: center; margin: 30px 0; }}
                .cta-text {{ font-size: 18px; color: #f2e2b1; margin-bottom: 20px; font-weight: 600; }}
                .download-buttons {{ display: flex; justify-content: center; gap: 15px; margin-top: 20px; }}
                .download-button {{ display: inline-block; background: linear-gradient(135deg, #f2e2b1, #d4c79e);
                          color: #1a1a2e; padding: 12px 24px; border-radius: 8px;
                          text-decoration: none; font-weight: 600; }}
                .echo-preview {{ background: rgba(242,226,177,0.1); border-radius: 8px;
                             padding: 20px; margin: 20px 0; text-align: center; border: 2px dashed #f2e2b1; }}
                .echo-icon {{ font-size: 48px; margin-bottom: 10px; }}
                .echo-title {{ font-size: 20px; color: #f2e2b1; margin-bottom: 5px; }}
                .echo-meta {{ font-size: 12px; color: #a3b3cc; }}
                .info-box {{ background: rgba(242,226,177,0.1); border-left: 3px solid #f2e2b1;
                            padding: 15px; margin: 20px 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="logo">Mirror Collective</div>
                </div>
                <div class="content">
                    <p>Hello <span class="highlight">{recipient_name}</span>,</p>
                    <p><strong>{sender_name}</strong> has created a meaningful message especially for you
                    through {self.app_name}.</p>

                    <div class="echo-preview">
                        <div class="echo-icon">{type_icon}</div>
                        <div class="echo-title">"{echo_title}"</div>
                        <div class="echo-meta">{echo_category} • {echo_type}</div>
                        <p style="margin-top: 15px; font-style: italic; color: #a3b3cc;">
                            This personal echo is waiting for you...
                        </p>
                    </div>

                    <div class="info-box">
                        <p><strong>To view this message, you'll need to download {self.app_name}:</strong></p>
                        <ol style="text-align: left; color: #fdfdf9;">
                            <li>Download the app using the buttons below</li>
                            <li>Sign up with this email address: <span class="highlight">{recipient_email}</span></li>
                            <li>Your echo will be waiting for you in your inbox</li>
                        </ol>
                    </div>

                    <div class="cta-section">
                        <div class="cta-text">Download {self.app_name} now</div>
                        <div class="download-buttons">
                            <a href="{self.app_store_url}" class="download-button">
                                📱 App Store
                            </a>
                            <a href="{self.play_store_url}" class="download-button">
                                🤖 Google Play
                            </a>
                        </div>
                    </div>

                    <p style="margin-top: 30px; text-align: center; color: #a3b3cc;">
                        {sender_name} chose to share something meaningful with you.
                        Join {self.app_name} to experience it.
                    </p>
                </div>
                <div class="footer">
                    <p>This message was sent from {self.app_name}, a space for meaningful connections.</p>
                    <p>If you didn't expect this message, you can safely ignore it.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_body = f"""
Hello {recipient_name},

{sender_name} has created a meaningful message especially for you through {self.app_name}.

{type_icon} "{echo_title}"
{echo_category} • {echo_type}

This personal echo is waiting for you...

TO VIEW THIS MESSAGE:
You'll need to download {self.app_name}:

1. Download the app:
   - iOS: {self.app_store_url}
   - Android: {self.play_store_url}

2. Sign up with this email address: {recipient_email}

3. Your echo will be waiting for you in your inbox

{sender_name} chose to share something meaningful with you. Join {self.app_name} to experience it.

---
This message was sent from {self.app_name}, a space for meaningful connections.
If you didn't expect this message, you can safely ignore it.
        """

        return await self._send_email(
            to_email=recipient_email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )

    async def send_echo_pending_notification(
        self,
        guardian_email: str,
        guardian_name: str,
        owner_name: str,
        echo_title: str,
        echo_category: str,
    ) -> bool:
        """
        Send notification to guardian when an echo is locked and awaiting their action.

        Args:
            guardian_email: Email address of the guardian
            guardian_name: Name of the guardian
            owner_name: Name of the echo creator
            echo_title: Title of the echo
            echo_category: Category of the echo

        Returns:
            True if email was sent successfully
        """
        subject = f"An Echo from {owner_name} is awaiting your action"

        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <style>
                body {{ font-family: 'Inter', Arial, sans-serif; background: #1a1a2e; color: #fdfdf9; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 40px 20px; }}
                .header {{ text-align: center; margin-bottom: 40px; }}
                .logo {{ color: #f2e2b1; font-size: 28px; font-family: 'Cormorant Garamond', serif; }}
                .content {{ background: rgba(255,255,255,0.05); border-radius: 12px; padding: 30px; }}
                .highlight {{ color: #f2e2b1; }}
                .footer {{ text-align: center; margin-top: 40px; color: #a3b3cc; font-size: 12px; }}
                .button {{ display: inline-block; background: linear-gradient(135deg, #f2e2b1, #d4c79e);
                          color: #1a1a2e; padding: 14px 28px; border-radius: 8px;
                          text-decoration: none; font-weight: 600; margin-top: 20px; }}
                .echo-card {{ background: rgba(242,226,177,0.1); border-radius: 8px;
                             padding: 20px; margin: 20px 0; }}
                .echo-title {{ font-size: 18px; color: #f2e2b1; margin-bottom: 5px; font-weight: 600; }}
                .echo-meta {{ font-size: 12px; color: #a3b3cc; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="logo">Mirror Collective</div>
                </div>
                <div class="content">
                    <p>Hello <span class="highlight">{guardian_name}</span>,</p>
                    <p><strong>{owner_name}</strong> has locked an echo that is now under your guardianship.</p>

                    <div class="echo-card">
                        <div class="echo-title">{echo_title}</div>
                        <div class="echo-meta">{echo_category}</div>
                    </div>

                    <p>As the guardian, you have the responsibility to manage when this echo is released
                    to its intended recipient. You can review pending echoes and take action when the time is right.</p>

                    <div style="text-align: center;">
                        <a href="{self.app_url}" class="button">View Pending Echoes</a>
                    </div>
                </div>
                <div class="footer">
                    <p>You are receiving this as a trusted Echo Guardian.</p>
                    <p>This is an automated message from {self.app_name}.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_body = f"""
Hello {guardian_name},

{owner_name} has locked an echo that is now under your guardianship.

Echo: {echo_title}
Category: {echo_category}

As the guardian, you have the responsibility to manage when this echo is released
to its intended recipient. You can review pending echoes and take action when
the time is right.

View Pending Echoes: {self.app_url}

---
You are receiving this as a trusted Echo Guardian.
This is an automated message from {self.app_name}.
        """

        return await self._send_email(
            to_email=guardian_email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )

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
                logger.info(f"Email sent to {to_email}, MessageId: {message_id}")
                return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error(f"SES error sending email to {to_email}: {error_code} - {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending email to {to_email}: {e}")
            return False


# Singleton instance
email_service = EmailService()
