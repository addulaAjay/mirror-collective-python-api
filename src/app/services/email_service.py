"""
Email Service for Echo Vault notifications.
Uses AWS SES to send emails for:
- Recipient invitations
- Guardian invitations
- Echo release notifications
"""

import logging
import os
from typing import Optional

import aioboto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class EmailService:
    """
    Service for sending Echo Vault notification emails via AWS SES.
    """

    def __init__(self):
        """Initialize Email service"""
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.sender_email = os.getenv(
            "SES_SENDER_EMAIL", "noreply@mirrorcollective.com"
        )
        self.app_name = os.getenv("APP_NAME", "Mirror Collective")
        self.app_url = os.getenv("APP_URL", "https://mirrorcollective.com")

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
    ) -> bool:
        """
        Send notification when an echo is released to a recipient.

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
        subject = f"You've received an Echo from {sender_name}"

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
                .button {{ display: inline-block; background: linear-gradient(135deg, #f2e2b1, #d4c79e);
                          color: #1a1a2e; padding: 14px 28px; border-radius: 8px;
                          text-decoration: none; font-weight: 600; margin-top: 20px; }}
                .echo-card {{ background: rgba(242,226,177,0.1); border-radius: 8px;
                             padding: 20px; margin: 20px 0; text-align: center; }}
                .echo-icon {{ font-size: 48px; margin-bottom: 10px; }}
                .echo-title {{ font-size: 20px; color: #f2e2b1; margin-bottom: 5px; }}
                .echo-meta {{ font-size: 12px; color: #a3b3cc; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <div class="logo">Mirror Collective</div>
                </div>
                <div class="content">
                    <p>Hello <span class="highlight">{recipient_name}</span>,</p>
                    <p><strong>{sender_name}</strong> has released an echo for you.</p>

                    <div class="echo-card">
                        <div class="echo-icon">{type_icon}</div>
                        <div class="echo-title">{echo_title}</div>
                        <div class="echo-meta">{echo_category} • {echo_type}</div>
                    </div>

                    <p>This message was created especially for you. Open the app to view it.</p>

                    <div style="text-align: center;">
                        <a href="{self.app_url}" class="button">View Echo</a>
                    </div>
                </div>
                <div class="footer">
                    <p>Echoes are meaningful messages shared through {self.app_name}.</p>
                </div>
            </div>
        </body>
        </html>
        """

        text_body = f"""
Hello {recipient_name},

{sender_name} has released an echo for you.

{type_icon} {echo_title}
{echo_category} • {echo_type}

This message was created especially for you. Open the app to view it.

View Echo: {self.app_url}

---
Echoes are meaningful messages shared through {self.app_name}.
        """

        return await self._send_email(
            to_email=recipient_email,
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
            async with self.session.client("ses", region_name=self.region) as ses:
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
