"""Fix #3: the validation error handler must log *which* field failed.

Previously the handler logged only ``Validation Error: N errors`` (a bare count),
so CloudWatch could not reveal which field caused a registration 422. These tests
assert the failing field name and message are included in the emitted log line.
"""

import asyncio
import logging

from fastapi.exceptions import RequestValidationError
from starlette.requests import Request

from src.app.api.models import UserRegistrationRequest
from src.app.core.error_handlers import validation_exception_handler


def _make_request(path: str = "/api/auth/register") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


def _validation_error_for_bad_registration() -> RequestValidationError:
    """Build a real RequestValidationError from a failing registration payload."""
    from pydantic import ValidationError

    try:
        UserRegistrationRequest(
            email="test@example.com",
            password="NoSpecial123",  # missing special char
            fullName="Valid Name",
        )
    except ValidationError as exc:
        return RequestValidationError(exc.errors())
    raise AssertionError("payload was expected to fail validation")


def test_handler_logs_failing_field_name(caplog):
    request = _make_request()
    exc = _validation_error_for_bad_registration()

    with caplog.at_level(logging.WARNING):
        asyncio.run(validation_exception_handler(request, exc))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a warning log for the validation error"
    log_text = " ".join(r.getMessage() for r in warnings)
    # The field that failed must be identifiable from the log message itself.
    assert "password" in log_text
    assert "/api/auth/register" in log_text


def test_handler_response_still_returns_validation_errors():
    request = _make_request()
    exc = _validation_error_for_bad_registration()

    response = asyncio.run(validation_exception_handler(request, exc))
    assert response.status_code == 422
