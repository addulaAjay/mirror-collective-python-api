"""
Comprehensive error handling for the FastAPI application
"""
import time
import logging
import traceback
import uuid
from typing import Any, Dict, Union

from fastapi import FastAPI, Request, Response, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import ValidationException, RequestValidationError
from pydantic import ValidationError

from .exceptions import BaseAPIException

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


async def base_api_exception_handler(request: Request, exc: BaseAPIException) -> JSONResponse:
    """Handler for BaseAPIException and its subclasses"""
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    is_development = request.app.debug or False
    
    # Log the error
    logger.error(
        f"API Exception: {exc.__class__.__name__}: {exc.message}",
        extra={
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
            "status_code": exc.status_code,
            "error_code": exc.error_code,
            "details": exc.details,
        }
    )
    
    response_data = {
        "success": False,
        "error": exc.message,
        "requestId": request_id,
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    
    # Add details in development mode
    if is_development and exc.details:
        response_data["details"] = exc.details
    
    # Add validation errors if present
    if hasattr(exc, 'details') and isinstance(exc.details, list):
        response_data["validationErrors"] = exc.details
    
    return JSONResponse(
        status_code=exc.status_code,
        content=response_data
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Handler for HTTPException"""
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    
    logger.warning(
        f"HTTP Exception: {exc.status_code}: {exc.detail}",
        extra={
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
            "status_code": exc.status_code,
        }
    )
    
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": exc.detail,
            "requestId": request_id,
            "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Handler for Pydantic validation errors"""
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    
    # Convert Pydantic errors to our format
    validation_errors = []
    for error in exc.errors():
        field_path = '.'.join(str(loc) for loc in error['loc']) if error['loc'] else 'unknown'
        validation_errors.append({
            "field": field_path,
            "message": error['msg']
        })
    
    logger.warning(
        f"Validation Error: {len(validation_errors)} errors",
        extra={
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
            "validation_errors": validation_errors,
        }
    )
    
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": "Validation Error",
            "message": "One or more fields contain invalid data",
            "validationErrors": validation_errors,
            "requestId": request_id,
            "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handler for unexpected exceptions"""
    request_id = getattr(request.state, 'request_id', str(uuid.uuid4()))
    is_development = request.app.debug or False
    
    # Log the full error
    logger.exception(
        f"Unhandled Exception: {exc.__class__.__name__}: {str(exc)}",
        extra={
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
        }
    )
    
    sanitized = sanitize_error(exc, is_development)
    
    response_data = {
        "success": False,
        "error": sanitized["message"],
        "requestId": request_id,
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    
    # Add debug information in development mode
    if is_development:
        if sanitized["details"]:
            response_data["details"] = sanitized["details"]
        if sanitized["stack"]:
            response_data["stack"] = sanitized["stack"]
    
    return JSONResponse(
        status_code=sanitized["status_code"],
        content=response_data
    )


def setup_error_handlers(app: FastAPI):
    """Setup all error handlers for the FastAPI app"""
    
    # Add custom exception handlers - use type: ignore to suppress mypy warnings
    # about complex exception handler signatures
    app.add_exception_handler(BaseAPIException, base_api_exception_handler)  # type: ignore
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore
    app.add_exception_handler(Exception, general_exception_handler)
    
    # Add request ID middleware
    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        
        start_time = time.time()
        response = await call_next(request)
        process_time = (time.time() - start_time) * 1000
        
        # Add request ID to response headers
        response.headers["X-Request-ID"] = request_id
        
        # Log request
        logger.info(
            f"{request.method} {request.url.path} - {response.status_code} - {process_time:.2f}ms",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "process_time_ms": process_time,
            }
        )
        
        return response