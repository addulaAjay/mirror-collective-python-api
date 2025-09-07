import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class ConversationTurn(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class MirrorChatRequest(BaseModel):
    message: str = Field(min_length=1)
    conversationHistory: Optional[List[ConversationTurn]] = None
    userName: Optional[str] = None


class MirrorChatResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]  # Contains reply and timestamp from use case


class UserRegistrationRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    fullName: str = Field(min_length=2, max_length=100, pattern=r"^[a-zA-Z\s\'-]+$")

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit")
        if not re.search(r"[@$!%*?&]", v):
            raise ValueError("Password must contain at least one special character")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    resetCode: str = Field(min_length=1)
    newPassword: str = Field(min_length=8)

    @field_validator("newPassword")
    @classmethod
    def validate_new_password(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters long")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit")
        if not re.search(r"[@$!%*?&]", v):
            raise ValueError("Password must contain at least one special character")
        return v


class RefreshTokenRequest(BaseModel):
    refreshToken: str = Field(min_length=1)


class EmailVerificationRequest(BaseModel):
    email: EmailStr
    verificationCode: str = Field(min_length=1)


class ResendVerificationCodeRequest(BaseModel):
    email: EmailStr


class TokenBundle(BaseModel):
    accessToken: str
    refreshToken: str


class UserBasic(BaseModel):
    id: str
    email: EmailStr
    fullName: str
    isVerified: bool


class AuthResponse(BaseModel):
    success: bool = True
    data: Optional[Dict[str, Any]] = None
    message: Optional[str] = None


class LoginResponse(BaseModel):
    success: bool = True
    data: Dict[str, Any]


class GeneralApiResponse(BaseModel):
    success: bool
    message: Optional[str] = None


class ErrorDetail(BaseModel):
    field: str
    message: str


class ValidationErrorResponse(BaseModel):
    success: bool = False
    error: str
    message: Optional[str] = None
    validationErrors: Optional[List[ErrorDetail]] = None
    requestId: Optional[str] = None
    timestamp: str


class ApiErrorResponse(BaseModel):
    success: bool = False
    error: str
    message: Optional[str] = None
    details: Optional[Any] = None
    requestId: Optional[str] = None
    timestamp: str


class HealthResponse(BaseModel):
    status: str
    service: str
    timestamp: str
