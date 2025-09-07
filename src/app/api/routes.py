from fastapi import APIRouter, Depends, Request
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)

from .models import (
    MirrorChatRequest, MirrorChatResponse, LoginRequest, LoginResponse, 
    UserRegistrationRequest, ForgotPasswordRequest,
    ResetPasswordRequest, RefreshTokenRequest, EmailVerificationRequest,
    ResendVerificationCodeRequest, AuthResponse, GeneralApiResponse
)
from ..core.security import get_current_user
from ..controllers.auth_controller import AuthController
from ..controllers.chat_controller import ChatController

router = APIRouter()

# Initialize controllers
auth_controller = AuthController()
chat_controller = ChatController()

# Auth endpoints

@router.post('/auth/register', response_model=AuthResponse, status_code=201)
async def register(payload: UserRegistrationRequest):
    """Register a new user account"""
    return await auth_controller.register(payload)

@router.post('/auth/login', response_model=LoginResponse)
async def login(payload: LoginRequest):
    """Authenticate user and return tokens"""
    return await auth_controller.login(payload)

@router.post('/auth/forgot-password', response_model=GeneralApiResponse)
async def forgot_password(payload: ForgotPasswordRequest):
    """Initiate password reset process"""
    return await auth_controller.forgot_password(payload)

@router.post('/auth/reset-password', response_model=GeneralApiResponse)
async def reset_password(payload: ResetPasswordRequest):
    """Reset password using verification code"""
    return await auth_controller.reset_password(payload)

@router.post('/auth/refresh', response_model=AuthResponse)
async def refresh_token(payload: RefreshTokenRequest):
    """Refresh access token using refresh token"""
    return await auth_controller.refresh_token(payload)

@router.post('/auth/confirm-email', response_model=GeneralApiResponse)
async def confirm_email(payload: EmailVerificationRequest):
    """Confirm email address with verification code"""
    return await auth_controller.confirm_email(payload)

@router.post('/auth/resend-verification-code', response_model=GeneralApiResponse)
async def resend_verification_code(payload: ResendVerificationCodeRequest):
    """Resend email verification code"""
    return await auth_controller.resend_verification_code(payload)

# Protected routes (require authentication)

@router.get('/auth/me')
async def get_current_user_profile(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Get current authenticated user profile"""
    return await auth_controller.get_current_user_profile(current_user)

@router.post('/auth/logout', response_model=GeneralApiResponse)
async def logout(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Logout current user and invalidate tokens"""
    return await auth_controller.logout(current_user)

@router.delete('/auth/account', response_model=GeneralApiResponse)
async def delete_account(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Delete current user account"""
    return await auth_controller.delete_account(current_user)

# Chat endpoints

@router.post('/chat/mirror', response_model=MirrorChatResponse)
async def mirror_chat(
    req: MirrorChatRequest,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Handle mirror chat requests"""
    # Log request headers for debugging token passing
    logger.info(f"Request headers: {dict(request.headers)}")
    auth_header = request.headers.get('authorization')
    logger.info(f"Authorization header: '{auth_header}'")
    
    return await chat_controller.handle_chat(req, current_user)
