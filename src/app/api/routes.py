import logging
import os
import re
import base64
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

logger = logging.getLogger(__name__)

from ..controllers.auth_controller import AuthController
from ..services.s3_service import S3Service
from ..services.dynamodb_service import DynamoDBService
from ..core.enhanced_auth import get_user_with_profile
from ..core.security import get_current_user
from .models import (
    AuthResponse,
    S3UploadWithUserRequest,
    S3TypedUploadResponse,
    S3GetObjectRequest,
    S3GetObjectResponse,
    S3PresignRequest,
    S3PresignResponse,
    EmailVerificationRequest,
    ForgotPasswordRequest,
    GeneralApiResponse,
    LoginRequest,
    LoginResponse,
    RefreshTokenRequest,
    ResendVerificationCodeRequest,
    ResetPasswordRequest,
    UserRegistrationRequest,
)

router = APIRouter()


def _decode_base64_field(b64_str: str) -> bytes:
    """Decode a base64 string that may include a data: URL prefix and missing padding.

    - Strips optional data URL header (e.g., data:audio/mpeg;base64,)
    - Removes whitespace/newlines
    - Adds missing '=' padding
    - Supports standard and URL-safe base64
    Raises ValueError on failure.
    """
    s = (b64_str or "").strip()
    if s.lower().startswith("data:"):
        # Keep only the part after the first comma
        parts = s.split(",", 1)
        s = parts[1] if len(parts) == 2 else ""

    # Remove all whitespace
    s = re.sub(r"\s+", "", s)
    if not s:
        raise ValueError("Empty base64 content")

    # Fix padding
    missing = (-len(s)) % 4
    if missing:
        s += "=" * missing

    try:
        if "-" in s or "_" in s:
            return base64.urlsafe_b64decode(s)
        else:
            return base64.b64decode(s, validate=False)
    except Exception as e:
        raise ValueError(f"Invalid base64 content: {e}")


# Dependency injection for controllers
def get_auth_controller():
    """Get auth controller instance (lazy initialization)"""
    return AuthController()


def get_s3_service():
    """Get S3 service instance (lazy initialization)"""
    return S3Service()


def get_dynamodb_service():
    return DynamoDBService()


# Auth endpoints
@router.post("/auth/register", response_model=AuthResponse, status_code=201)
async def register(
    payload: UserRegistrationRequest, auth_controller=Depends(get_auth_controller)
):
    """Register a new user account"""
    return await auth_controller.register(payload)


# Storage endpoints
@router.post("/storage/future-text-message", response_model=S3TypedUploadResponse)
async def save_text_note(
    payload: S3UploadWithUserRequest,
    s3_service=Depends(get_s3_service),
    db_service=Depends(get_dynamodb_service),
):
    bucket = payload.bucketName or os.getenv("S3_TEXT_BUCKET")
    if not bucket:
        raise ValueError("Missing S3_TEXT_BUCKET")
    result = s3_service.upload_text(
        content=payload.content,
        bucket_name=bucket,
        key=payload.key,
        content_type=payload.contentType or "text/plain",
        acl=payload.acl,
        metadata=payload.metadata,
    )
    vault = await db_service.record_echo_vault_entry(
        user_id=payload.userId,
        media_type="text",
        s3_bucket=result["bucket"],
        s3_key=result["key"],
        object_url=result["objectUrl"],
        content_type=result["contentType"],
    )
    out = {**result, "vaultId": vault["vault_id"]}
    return S3TypedUploadResponse(success=True, data=out, message="Text saved")


@router.post("/storage/future-voice-message", response_model=S3TypedUploadResponse)
async def save_voice_note(
    payload: S3UploadWithUserRequest,
    s3_service=Depends(get_s3_service),
    db_service=Depends(get_dynamodb_service),
):
    bucket = payload.bucketName or os.getenv("S3_VOICE_BUCKET")
    if not bucket:
        raise ValueError("Missing S3_VOICE_BUCKET (provide bucketName to override)")
    # Expect content to be base64 for voice (data URL or raw). Normalize + decode.
    try:
        data = _decode_base64_field(payload.content)
    except ValueError as e:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=str(e))
    result = s3_service.upload_bytes(
        data=data,
        bucket_name=bucket,
        key=payload.key,
        content_type=payload.contentType or "audio/mpeg",
        acl=payload.acl,
        metadata=payload.metadata,
    )
    vault = await db_service.record_echo_vault_entry(
        user_id=payload.userId,
        media_type="voice",
        s3_bucket=result["bucket"],
        s3_key=result["key"],
        object_url=result["objectUrl"],
        content_type=result["contentType"],
    )
    out = {**result, "vaultId": vault["vault_id"]}
    return S3TypedUploadResponse(success=True, data=out, message="Voice saved")


@router.post("/storage/future-video-message", response_model=S3TypedUploadResponse)
async def save_video_note(
    payload: S3UploadWithUserRequest,
    s3_service=Depends(get_s3_service),
    db_service=Depends(get_dynamodb_service),
):
    bucket = payload.bucketName or os.getenv("S3_VIDEO_BUCKET")
    if not bucket:
        raise ValueError("Missing S3_VIDEO_BUCKET (provide bucketName to override)")
    # Expect content to be base64 for video (data URL or raw). Normalize + decode.
    try:
        data = _decode_base64_field(payload.content)
    except ValueError as e:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail=str(e))
    result = s3_service.upload_bytes(
        data=data,
        bucket_name=bucket,
        key=payload.key,
        content_type=payload.contentType or "video/mp4",
        acl=payload.acl,
        metadata=payload.metadata,
    )
    vault = await db_service.record_echo_vault_entry(
        user_id=payload.userId,
        media_type="video",
        s3_bucket=result["bucket"],
        s3_key=result["key"],
        object_url=result["objectUrl"],
        content_type=result["contentType"],
    )
    out = {**result, "vaultId": vault["vault_id"]}
    return S3TypedUploadResponse(success=True, data=out, message="Video saved")


@router.post("/storage/get-object", response_model=S3GetObjectResponse)
async def get_object(
    payload: S3GetObjectRequest, s3_service=Depends(get_s3_service)
):
    """Fetch an object from S3 and return the text and metadata."""
    data = s3_service.get_text(key=payload.key, bucket_name=payload.bucketName)
    return S3GetObjectResponse(success=True, data=data)


@router.post("/storage/presign-get", response_model=S3PresignResponse)
async def presign_get(
    payload: S3PresignRequest, s3_service=Depends(get_s3_service)
):
    """Generate a pre-signed GET URL for an S3 object."""
    url = s3_service.generate_presigned_get_url(
        key=payload.key,
        bucket_name=payload.bucketName,
        expires_in=payload.expiresIn,
    )
    return S3PresignResponse(success=True, url=url)


@router.post("/auth/login", response_model=LoginResponse)
async def login(payload: LoginRequest, auth_controller=Depends(get_auth_controller)):
    """Authenticate user and return tokens"""
    return await auth_controller.login(payload)


@router.post("/auth/forgot-password", response_model=GeneralApiResponse)
async def forgot_password(
    payload: ForgotPasswordRequest, auth_controller=Depends(get_auth_controller)
):
    """Initiate password reset process"""
    return await auth_controller.forgot_password(payload)


@router.post("/auth/reset-password", response_model=GeneralApiResponse)
async def reset_password(
    payload: ResetPasswordRequest, auth_controller=Depends(get_auth_controller)
):
    """Reset password using verification code"""
    return await auth_controller.reset_password(payload)


@router.post("/auth/refresh", response_model=AuthResponse)
async def refresh_token(
    payload: RefreshTokenRequest, auth_controller=Depends(get_auth_controller)
):
    """Refresh access token using refresh token"""
    return await auth_controller.refresh_token(payload)


@router.post("/auth/confirm-email", response_model=GeneralApiResponse)
async def confirm_email(
    payload: EmailVerificationRequest, auth_controller=Depends(get_auth_controller)
):
    """Confirm email address with verification code"""
    return await auth_controller.confirm_email(payload)


@router.post("/auth/resend-verification-code", response_model=GeneralApiResponse)
async def resend_verification_code(
    payload: ResendVerificationCodeRequest, auth_controller=Depends(get_auth_controller)
):
    """Resend email verification code"""
    return await auth_controller.resend_verification_code(payload)


@router.get("/auth/me")
async def get_current_user_profile(
    current_user: Dict[str, Any] = Depends(get_user_with_profile),
    auth_controller=Depends(get_auth_controller),
):
    """Get current authenticated user profile"""
    return await auth_controller.get_current_user_profile(current_user)


@router.post("/auth/logout", response_model=GeneralApiResponse)
async def logout(
    current_user: Dict[str, Any] = Depends(get_current_user),
    auth_controller=Depends(get_auth_controller),
):
    """Logout current user and invalidate tokens"""
    return await auth_controller.logout(current_user)


@router.delete("/auth/account", response_model=GeneralApiResponse)
async def delete_account(
    current_user: Dict[str, Any] = Depends(get_current_user),
    auth_controller=Depends(get_auth_controller),
):
    """Delete current user account"""
    return await auth_controller.delete_account(current_user)


# Note: Chat functionality has been moved to MirrorGPT routes at /api/mirrorgpt/chat
# This provides the full MirrorGPT experience with 5-signal analysis and archetype guidance
