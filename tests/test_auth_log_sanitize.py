"""Auth logging sanitization.

Keeps PII (email) and raw Cognito error text out of production logs:
  - mask_email masks the local part of an address.
  - Cognito error handling logs the (safe) error code at ERROR and demotes the
    raw Cognito message — which can echo identifiers — to DEBUG.
"""

import logging

import pytest
from botocore.exceptions import ClientError

from src.app.core.exceptions import AuthenticationError
from src.app.core.log_sanitize import mask_email
from src.app.services.cognito_service import CognitoService


class TestMaskEmail:
    def test_masks_local_part_keeps_domain(self):
        assert mask_email("john.doe@example.com") == "j***@example.com"

    def test_single_char_local(self):
        assert mask_email("a@example.com") == "a***@example.com"

    @pytest.mark.parametrize("value", [None, "", "not-an-email", "@nolocal.com", 123])
    def test_unusable_input_is_redacted(self, value):
        assert mask_email(value) == "<redacted>"


class TestCognitoErrorLogging:
    def _error(self, code, message):
        return ClientError(
            {"Error": {"Code": code, "Message": message}}, "InitiateAuth"
        )

    def test_error_code_logged_but_not_raw_message(self, caplog):
        svc = CognitoService()
        secret = "user a3f9 in pool xyz failed: token leaked detail"

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(AuthenticationError):
                svc._handle_cognito_error(
                    self._error("NotAuthorizedException", secret), "login"
                )

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        error_text = " ".join(r.getMessage() for r in error_records)
        assert "NotAuthorizedException" in error_text  # safe code is logged
        assert secret not in error_text  # raw message is NOT at ERROR
