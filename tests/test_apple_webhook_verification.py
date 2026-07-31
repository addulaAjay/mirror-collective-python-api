"""Apple App Store Server Notification signature verification.

Security-critical: a bad signature MUST raise JWSVerificationError so the
webhook handler fails closed (never mutates entitlements). The SDK verifier +
the unverified peek are mocked so these tests need no real Apple keys.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.app.services import receipt_validator as rv
from src.app.services import subscription_service as ss
from src.app.services.receipt_validator import (
    JWSVerificationError,
    verify_and_decode_apple_notification,
    verify_apple_transaction_jws,
)


def _notification(env="Production"):
    return {
        "notificationType": "DID_RENEW",
        "data": {
            "environment": env,
            "signedTransactionInfo": "jws.transaction",
        },
    }


def test_notification_verifies_and_returns_dict_production():
    verifier = MagicMock()
    with (
        patch.object(
            rv, "_decode_jws_payload", return_value=_notification("Production")
        ),
        patch.object(
            rv, "_get_apple_signed_data_verifier", return_value=verifier
        ) as get_verifier,
    ):
        payload, sandbox = verify_and_decode_apple_notification("signed.payload")

    assert sandbox is False
    assert payload["notificationType"] == "DID_RENEW"
    get_verifier.assert_called_once_with(sandbox=False)
    verifier.verify_and_decode_notification.assert_called_once_with("signed.payload")


def test_notification_detects_sandbox_environment():
    verifier = MagicMock()
    with (
        patch.object(rv, "_decode_jws_payload", return_value=_notification("Sandbox")),
        patch.object(
            rv, "_get_apple_signed_data_verifier", return_value=verifier
        ) as get_verifier,
    ):
        _, sandbox = verify_and_decode_apple_notification("signed.payload")

    assert sandbox is True
    get_verifier.assert_called_once_with(sandbox=True)


def test_notification_bad_signature_raises_fail_closed():
    from appstoreserverlibrary.signed_data_verifier import (
        VerificationException,
        VerificationStatus,
    )

    verifier = MagicMock()
    verifier.verify_and_decode_notification.side_effect = VerificationException(
        VerificationStatus.VERIFICATION_FAILURE
    )
    with (
        patch.object(rv, "_decode_jws_payload", return_value=_notification()),
        patch.object(rv, "_get_apple_signed_data_verifier", return_value=verifier),
    ):
        with pytest.raises(JWSVerificationError):
            verify_and_decode_apple_notification("tampered.payload")


def test_notification_empty_payload_raises():
    with pytest.raises(JWSVerificationError):
        verify_and_decode_apple_notification("")


def test_verify_apple_transaction_jws_delegates_with_sandbox_flag():
    with patch.object(
        rv, "_verify_apple_jws", return_value={"transactionId": "t1"}
    ) as vj:
        result = verify_apple_transaction_jws("jws.tx", sandbox=True)
    assert result == {"transactionId": "t1"}
    vj.assert_called_once_with("jws.tx", sandbox=True)


@pytest.mark.asyncio
async def test_handle_apple_webhook_fails_closed_on_bad_signature():
    """A tampered notification must not process — return success: False."""
    svc = ss.SubscriptionService(MagicMock())
    with patch.object(
        ss,
        "verify_and_decode_apple_notification",
        side_effect=JWSVerificationError("bad signature"),
    ):
        result = await svc.handle_apple_webhook({"signedPayload": "tampered"})

    assert result["success"] is False
    assert "signature" in result["error"].lower()
