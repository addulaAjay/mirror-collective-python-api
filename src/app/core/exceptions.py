"""
Custom exception classes for the API
"""

from typing import Any, Dict, List, Optional


class BaseAPIException(Exception):
    """Base class for all API exceptions"""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        error_code: Optional[str] = None,
        details: Optional[Any] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.details = details


class AuthenticationError(BaseAPIException):
    """Raised when authentication fails"""

    def __init__(
        self, message: str = "Authentication failed", details: Optional[Any] = None
    ):
        super().__init__(message, 401, "AUTHENTICATION_ERROR", details)


class ValidationError(BaseAPIException):
    """Raised when request validation fails"""

    def __init__(
        self,
        message: str = "Validation error",
        validation_errors: Optional[List[Dict[str, str]]] = None,
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
        status_code: int = 500,
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


# ============================================================
# Reflection Room V1 — see spec §12
# ============================================================


class InvalidQuizAnswer(BaseAPIException):
    """Raised when a quiz answer enum is invalid or a question is missing."""

    def __init__(self, message: str = "Invalid quiz answer"):
        super().__init__(message, 400, "INVALID_QUIZ_ANSWER")


class LoopNotSupported(BaseAPIException):
    """Raised when an unsupported loop_id is provided."""

    def __init__(self, message: str = "Loop not supported in V1"):
        super().__init__(message, 400, "LOOP_NOT_SUPPORTED")


class MotifNotFound(BaseAPIException):
    """Raised when a motif_id is missing from motif_mapping.v1.json."""

    def __init__(self, message: str = "Motif not found"):
        super().__init__(message, 400, "MOTIF_NOT_FOUND")


class OverrideNotAllowed(BaseAPIException):
    """Raised when room-skin override is attempted but quiz had a unique winner."""

    def __init__(self, message: str = "Override not allowed for this session"):
        super().__init__(message, 403, "OVERRIDE_NOT_ALLOWED")


class NoActiveLoops(BaseAPIException):
    """Raised when /echo/recommend-practice has no active loops to choose from."""

    def __init__(self, message: str = "No active loops in snapshot"):
        super().__init__(message, 404, "NO_ACTIVE_LOOPS")


class NoRuleMatched(BaseAPIException):
    """Only fires when fallback_enabled=False. With V1 default, never reaches FE."""

    def __init__(self, message: str = "No rule matched the active loop"):
        super().__init__(message, 404, "NO_RULE_MATCHED")


class AllCandidatesFiltered(BaseAPIException):
    """Only fires when fallback_enabled=False. Carries Retry-After hint."""

    def __init__(
        self,
        message: str = "All candidate practices filtered by safety/cooldown",
        retry_after_seconds: int = 3600,
    ):
        super().__init__(message, 409, "ALL_CANDIDATES_FILTERED")
        self.retry_after_seconds = retry_after_seconds


class FallbackOnCooldown(BaseAPIException):
    """Genuine 'no practice for you right now' — fallback itself is on cooldown."""

    def __init__(
        self,
        message: str = "Fallback practice is within cooldown",
        retry_after_seconds: int = 3600,
    ):
        super().__init__(message, 409, "FALLBACK_ON_COOLDOWN")
        self.retry_after_seconds = retry_after_seconds


class OverrideTagNotInTie(BaseAPIException):
    """Raised when user_override_tag is not part of the tied set."""

    def __init__(self, message: str = "Override tag not in tied set"):
        super().__init__(message, 409, "OVERRIDE_TAG_NOT_IN_TIE")


class ConfigLoadError(BaseAPIException):
    """Raised when a YAML/JSON config file fails to parse at startup or first read."""

    def __init__(self, message: str = "Configuration file load failed"):
        super().__init__(message, 500, "CONFIG_LOAD_ERROR")


class SessionExpired(BaseAPIException):
    """Raised when the user's reflection session has expired (past midnight in
    user_tz). FE should render the 'take the quiz' affordance.

    Distinguished from ``NotFoundError`` so clients can switch on errorCode:
      * ``SESSION_EXPIRED`` (this) — user had a session that aged out
      * ``NOT_FOUND`` — user has never started a session
    Both return HTTP 404; FE can show "take the quiz" for either.
    """

    def __init__(
        self,
        message: str = "Reflection session has expired; take the quiz again",
    ):
        super().__init__(message, 404, "SESSION_EXPIRED")
