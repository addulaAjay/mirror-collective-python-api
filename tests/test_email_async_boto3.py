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
    """The module-level _SES_CLIENT_CONFIG must be tuned.

    Robust to botocore's Config normalization: depending on whether the
    Config has been merged with another (which happens when other tests
    in the suite construct boto3 clients), the ``retries`` dict may
    surface ``max_attempts`` (the value we set), ``total_max_attempts``
    (max_attempts + 1, what botocore actually uses internally), or both.
    We accept either as long as the mode is adaptive and the attempts
    value is what we configured.
    """
    from src.app.services.email_service import _SES_CLIENT_CONFIG

    # botocore.Config sets attributes via __setattr__; they're not in type stubs.
    assert _SES_CLIENT_CONFIG.max_pool_connections == 50  # type: ignore[attr-defined]
    retries = _SES_CLIENT_CONFIG.retries  # type: ignore[attr-defined]
    assert retries.get("mode") == "adaptive"
    # Either form is acceptable; pick whichever botocore exposed.
    max_attempts = retries.get("max_attempts")
    total_max_attempts = retries.get("total_max_attempts")
    assert max_attempts == 5 or total_max_attempts == 6, (
        f"Expected max_attempts=5 or total_max_attempts=6, got "
        f"max_attempts={max_attempts!r}, total_max_attempts={total_max_attempts!r}"
    )


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


def _mock_ses_cm(send_email_mock):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=AsyncMock(send_email=send_email_mock))
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


@pytest.mark.asyncio
async def test_send_email_masks_recipient_on_success(caplog):
    """The recipient address must be masked in the success log line (no PII)."""
    import logging

    from src.app.services.email_service import EmailService

    service = EmailService()
    send = AsyncMock(return_value={"MessageId": "abc-123"})

    with patch.object(service.session, "client", return_value=_mock_ses_cm(send)):
        with caplog.at_level(logging.INFO):
            ok = await service._send_email(
                to_email="john.doe@example.com",
                subject="Subj",
                html_body="<p>Hi</p>",
                text_body="Hi",
            )

    assert ok is True
    logs = " ".join(r.getMessage() for r in caplog.records)
    assert "john.doe@example.com" not in logs
    assert "j***@example.com" in logs


@pytest.mark.asyncio
async def test_send_email_masks_recipient_on_error(caplog):
    """The recipient address must be masked in the error log line too."""
    import logging

    from botocore.exceptions import ClientError

    from src.app.services.email_service import EmailService

    service = EmailService()
    send = AsyncMock(
        side_effect=ClientError(
            {"Error": {"Code": "MessageRejected", "Message": "bad"}}, "SendEmail"
        )
    )

    with patch.object(service.session, "client", return_value=_mock_ses_cm(send)):
        with caplog.at_level(logging.ERROR):
            ok = await service._send_email(
                to_email="john.doe@example.com",
                subject="Subj",
                html_body="<p>Hi</p>",
                text_body="Hi",
            )

    assert ok is False
    logs = " ".join(r.getMessage() for r in caplog.records)
    assert "john.doe@example.com" not in logs
    assert "j***@example.com" in logs
