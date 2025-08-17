"""
Custom exception classes for the API
"""
from typing import Any, Optional, List, Dict


class BaseAPIException(Exception):
    """Base class for all API exceptions"""
    
    def __init__(
        self, 
        message: str, 
        status_code: int = 500,
        error_code: Optional[str] = None,
        details: Optional[Any] = None
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.details = details


class AuthenticationError(BaseAPIException):
    """Raised when authentication fails"""
    
    def __init__(self, message: str = "Authentication failed", details: Optional[Any] = None):
        super().__init__(message, 401, "AUTHENTICATION_ERROR", details)


class ValidationError(BaseAPIException):
    """Raised when request validation fails"""
    
    def __init__(
        self, 
        message: str = "Validation error", 
        validation_errors: Optional[List[Dict[str, str]]] = None
    ):
        super().__init__(message, 400, "VALIDATION_ERROR", validation_errors)


class UserNotFoundError(BaseAPIException):
    """Raised when user is not found"""
    
    def __init__(self, message: str = "User not found"):
        super().__init__(message, 404, "USER_NOT_FOUND")


class UserAlreadyExistsError(BaseAPIException):
    """Raised when trying to create a user that already exists"""
    
    def __init__(self, message: str = "User already exists"):
        super().__init__(message, 409, "USER_ALREADY_EXISTS")


class TokenExpiredError(BaseAPIException):
    """Raised when a token has expired"""
    
    def __init__(self, message: str = "Token has expired"):
        super().__init__(message, 401, "TOKEN_EXPIRED")


class InvalidTokenError(BaseAPIException):
    """Raised when a token is invalid"""
    
    def __init__(self, message: str = "Invalid token"):
        super().__init__(message, 401, "INVALID_TOKEN")


class RateLimitExceededError(BaseAPIException):
    """Raised when rate limit is exceeded"""
    
    def __init__(self, message: str = "Rate limit exceeded", retry_after: int = 900):
        super().__init__(message, 429, "RATE_LIMIT_EXCEEDED")
        self.retry_after = retry_after


class CognitoServiceError(BaseAPIException):
    """Raised when Cognito service operation fails"""
    
    def __init__(
        self, 
        message: str, 
        cognito_error_code: Optional[str] = None, 
        status_code: int = 500
    ):
        super().__init__(message, status_code, "COGNITO_SERVICE_ERROR")
        self.cognito_error_code = cognito_error_code


class EmailServiceError(BaseAPIException):
    """Raised when email service operation fails"""
    
    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message, status_code, "EMAIL_SERVICE_ERROR")


class InternalServerError(BaseAPIException):
    """Raised for internal server errors"""
    
    def __init__(self, message: str = "Internal server error"):
        super().__init__(message, 500, "INTERNAL_SERVER_ERROR")


class NotFoundError(BaseAPIException):
    """Raised when a resource is not found"""
    
    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, 404, "NOT_FOUND")


class ForbiddenError(BaseAPIException):
    """Raised when access is forbidden"""
    
    def __init__(self, message: str = "Access forbidden"):
        super().__init__(message, 403, "FORBIDDEN")