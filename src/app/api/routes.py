import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request

logger = logging.getLogger(__name__)

from ..controllers.auth_controller import AuthController
from ..controllers.chat_controller import ChatController
from ..core.security import get_current_user
from .models import (
    AuthResponse,
    EmailVerificationRequest,
    ForgotPasswordRequest,
    GeneralApiResponse,
    LoginRequest,
    LoginResponse,
    MirrorChatRequest,
    MirrorChatResponse,
    RefreshTokenRequest,
    ResendVerificationCodeRequest,
    ResetPasswordRequest,
    UserRegistrationRequest,
)

router = APIRouter()


# Dependency injection for controllers
def get_auth_controller():
    """Get auth controller instance (lazy initialization)"""
    return AuthController()


def get_chat_controller():
    """Get chat controller instance (lazy initialization)"""
    return ChatController()


# Auth endpoints
@router.post("/auth/register", response_model=AuthResponse, status_code=201)
async def register(
    payload: UserRegistrationRequest, auth_controller=Depends(get_auth_controller)
):
    """Register a new user account"""
    return await auth_controller.register(payload)


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
    current_user: Dict[str, Any] = Depends(get_current_user),
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


# Chat endpoints
@router.post("/chat", response_model=MirrorChatResponse)
async def mirror_chat(
    req: MirrorChatRequest,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
    chat_controller=Depends(get_chat_controller),
):
    """Handle mirror chat requests"""
    # Log request headers for debugging token passing
    logger.info(f"Request headers: {dict(request.headers)}")
    auth_header = request.headers.get("authorization")
    logger.info(f"Authorization header: '{auth_header}'")

    return await chat_controller.handle_chat(req, current_user)


# Include enhanced routes after the main router is defined
try:
    from .enhanced_routes import enhanced_chat_router

    router.include_router(enhanced_chat_router)
    logger.info("Enhanced chat routes included successfully")
except Exception as e:
    logger.warning(f"Could not include enhanced routes: {e}")
    # Continue without enhanced routes for now
