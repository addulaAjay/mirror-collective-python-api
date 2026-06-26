"""
Comprehensive error handling for the FastAPI application
"""

import logging
import time
import traceback
import uuid
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import Response

from .exceptions import BaseAPIException
from .request_context import (
    reset_request_id,
    reset_user_id,
    set_request_id,
    set_user_id,
)

logger = logging.getLogger(__name__)


def sanitize_error(error: Exception, is_development: bool = False) -> Dict[str, Any]:
    """
    Sanitize error messages to prevent information disclosure
    """
    # Define safe error messages for production
    safe_errors = {
        "ValidationError": "Invalid input provided",
        "CastError": "Invalid data format",
        "AuthenticationError": "Authentication failed",
        "TokenExpiredError": "Session expired",
        "InvalidTokenError": "Authentication failed",
        "NotBeforeError": "Authentication failed",
        "AuthorizationError": "Access denied",
        "RateLimitError": "Rate limit exceeded",
        "CognitoError": "Authentication service error",
        "AwsError": "Service temporarily unavailable",
    }

    # Determine status code
    if isinstance(error, BaseAPIException):
        status_code = error.status_code
        message = error.message
        details = error.details if is_development else None
    elif isinstance(error, HTTPException):
        status_code = error.status_code
        message = error.detail
        details = None
    else:
        status_code = 500
        message = str(error)
        details = None

    # Sanitize message for production
    if not is_development:
        if status_code >= 500:
            message = "Internal server error"
        elif status_code >= 400:
            error_name = error.__class__.__name__
            message = safe_errors.get(error_name, "Bad request")

    return {
        "status_code": status_code,
        "message": message,
        "details": details,
        "stack": traceback.format_exc() if is_development else None,
    }


async def base_api_exception_handler(request: Request, exc: Exception) -> Response:
    """Handler for BaseAPIException and its subclasses"""
    # Type check - ensure we're dealing with a BaseAPIException
    if not isinstance(exc, BaseAPIException):
        # Fallback to general handler
        return await general_exception_handler(request, exc)

    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    is_development = request.app.debug or False

    # Log the error
    logger.error(
        f"API Exception: {exc.__class__.__name__}: {exc.message}",
        extra={
            "path": request.url.path,
            "method": request.method,
            "status_code": exc.status_code,
            "error_code": exc.error_code,
            "details": exc.details,
        },
    )

    # 5xx messages must never be sent to clients verbatim — they often contain
    # stack frames, AWS SDK error strings, or internal identifiers. Replace
    # with a generic message; the real text is in the logs above.
    user_facing_message = exc.message
    if exc.status_code >= 500 and not is_development:
        user_facing_message = "Something went wrong. Please try again."

    response_data: Dict[str, Any] = {
        "success": False,
        "error": user_facing_message,
        "requestId": request_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Surface stable error code so clients can switch on it (spec §12).
    if exc.error_code:
        response_data["errorCode"] = exc.error_code

    # Add details in development mode
    if is_development and exc.details:
        response_data["details"] = exc.details

    # Add validation errors if present
    if hasattr(exc, "details") and isinstance(exc.details, list):
        response_data["validationErrors"] = exc.details

    headers: Dict[str, str] = {}
    retry = getattr(exc, "retry_after_seconds", None)
    if retry is not None:
        headers["Retry-After"] = str(int(retry))

    return JSONResponse(
        status_code=exc.status_code, content=response_data, headers=headers
    )


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    """Handler for HTTPException"""
    # Type check - ensure we're dealing with an HTTPException
    if not isinstance(exc, HTTPException):
        # Fallback to general handler
        return await general_exception_handler(request, exc)

    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    logger.warning(
        f"HTTP Exception: {exc.status_code}: {exc.detail}",
        extra={
            "path": request.url.path,
            "method": request.method,
            "status_code": exc.status_code,
        },
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": exc.detail,
            "requestId": request_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def validation_exception_handler(request: Request, exc: Exception) -> Response:
    """Handler for Pydantic validation errors"""
    # Type check - ensure we're dealing with a RequestValidationError
    if not isinstance(exc, RequestValidationError):
        # Fallback to general handler
        return await general_exception_handler(request, exc)

    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    # Convert Pydantic errors to our format
    validation_errors = []
    for error in exc.errors():
        field_path = (
            ".".join(str(loc) for loc in error["loc"]) if error["loc"] else "unknown"
        )
        validation_errors.append({"field": field_path, "message": error["msg"]})

    # Include the failing field(s) in the message itself so they are visible in
    # log aggregators (e.g. CloudWatch) that don't render the structured `extra`.
    error_summary = "; ".join(
        f"{e['field']}: {e['message']}" for e in validation_errors
    )
    logger.warning(
        f"Validation Error on {request.method} {request.url.path}: "
        f"{len(validation_errors)} error(s) - {error_summary}",
        extra={
            "path": request.url.path,
            "method": request.method,
            "validation_errors": validation_errors,
        },
    )

    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": "Validation Error",
            "message": "One or more fields contain invalid data",
            "validationErrors": validation_errors,
            "requestId": request_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


async def general_exception_handler(request: Request, exc: Exception) -> Response:
    """Handler for unexpected exceptions"""
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    is_development = request.app.debug or False

    # Log the full error
    logger.exception(
        f"Unhandled Exception: {exc.__class__.__name__}: {str(exc)}",
        extra={
            "path": request.url.path,
            "method": request.method,
        },
    )

    sanitized = sanitize_error(exc, is_development)

    response_data = {
        "success": False,
        "error": sanitized["message"],
        "requestId": request_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # Add debug information in development mode
    if is_development:
        if sanitized["details"]:
            response_data["details"] = sanitized["details"]
        if sanitized["stack"]:
            response_data["stack"] = sanitized["stack"]

    return JSONResponse(status_code=sanitized["status_code"], content=response_data)


def setup_error_handlers(app: FastAPI):
    """Setup all error handlers for the FastAPI app"""

    # Add custom exception handlers
    app.add_exception_handler(BaseAPIException, base_api_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, general_exception_handler)

    # Add request ID middleware
    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        # Honor an inbound X-Request-ID so a trace can span services/clients;
        # otherwise generate one. The id is bound into the logging context so
        # every log line for this request carries it.
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id

        request_id_token = set_request_id(request_id)
        # Reset user_id to default at request start; the auth dependency sets
        # the real value once the token is decoded. Resetting here prevents a
        # previous request's user from leaking into an unauthenticated one.
        user_id_token = set_user_id(None)

        start_time = time.time()
        try:
            response = await call_next(request)
            process_time = (time.time() - start_time) * 1000

            # Add request ID to response headers
            response.headers["X-Request-ID"] = request_id

            # Log request (still inside the bound context so the line carries
            # this request's id/user).
            logger.info(
                f"{request.method} {request.url.path} - "
                f"{response.status_code} - {process_time:.2f}ms",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "process_time_ms": process_time,
                },
            )

            return response
        finally:
            reset_user_id(user_id_token)
            reset_request_id(request_id_token)
