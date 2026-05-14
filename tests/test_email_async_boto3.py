"""
Tests for the EmailService SES client configuration.

EmailService already uses aioboto3 (truly async). What this PR added is a
tuned botocore Config (max_pool_connections=50, adaptive retries) applied
to every SES client created from the long-lived aioboto3 Session, so
burst email traffic doesn't saturate the default 10-connection pool.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_ses_client_config_has_max_pool_connections():
    """The module-level _SES_CLIENT_CONFIG must be tuned."""
    from src.app.services.email_service import _SES_CLIENT_CONFIG

    # botocore.Config sets attributes via __setattr__; they're not in type stubs.
    assert _SES_CLIENT_CONFIG.max_pool_connections == 50  # type: ignore[attr-defined]
    assert _SES_CLIENT_CONFIG.retries == {"max_attempts": 5, "mode": "adaptive"}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_send_email_passes_config_to_session_client():
    """
    _send_email must construct the SES client with the tuned config so the
    pool / retry tuning actually propagates per send.
    """
    from src.app.services.email_service import _SES_CLIENT_CONFIG, EmailService

    service = EmailService()

    # Build a chain of mocks that satisfies:
    #   async with self.session.client(...) as ses:
    #       await ses.send_email(...)
    mock_ses = AsyncMock()
    mock_ses.send_email = AsyncMock(return_value={"MessageId": "abc-123"})

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_ses)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch.object(service.session, "client", return_value=cm) as mock_client:
        ok = await service._send_email(
            to_email="u@example.com",
            subject="Subj",
            html_body="<p>Hi</p>",
            text_body="Hi",
        )

    assert ok is True
    mock_client.assert_called_once()
    args, kwargs = mock_client.call_args
    assert args[0] == "ses"
    assert kwargs["region_name"] == service.region
    # Crucially: the tuned config must propagate.
    assert kwargs["config"] is _SES_CLIENT_CONFIG

    # And the actual SES send call must have happened with the right body.
    mock_ses.send_email.assert_called_once()
    send_kwargs = mock_ses.send_email.call_args.kwargs
    assert send_kwargs["Source"] == service.sender_email
    assert send_kwargs["Destination"]["ToAddresses"] == ["u@example.com"]
    assert send_kwargs["Message"]["Subject"]["Data"] == "Subj"
