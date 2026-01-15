import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends

from ..controllers.auth_controller import AuthController
from ..core.enhanced_auth import get_user_with_profile
from ..core.security import get_current_user
from ..services.sns_service import SNSService
from .models import (
    AuthResponse,
    DeviceRegistrationRequest,
    EmailVerificationRequest,
    ForgotPasswordRequest,
    GeneralApiResponse,
    LoginRequest,
    LoginResponse,
    NotificationRequest,
    RefreshTokenRequest,
    ResendVerificationCodeRequest,
    ResetPasswordRequest,
    UserRegistrationRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Initialize controllers
auth_controller = AuthController()
sns_service = SNSService()


# Dependency injection for controllers
def get_auth_controller():
    """Provide shared auth controller instance"""
    return auth_controller


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


# push notification endpoint
@router.post("/send-notification")
async def send_notification(request: NotificationRequest):
    msg_id = sns_service.publish_to_topic(request.title, request.body)
    return {"status": "sent", "message_id": msg_id}

# this is the FCM token you get from the mobile app
@router.post("/register-device")
def register_device(request: DeviceRegistrationRequest):
    # Step 1: Try to find an existing endpoint for this token
    try:
        response = sns_service.sns.list_endpoints_by_platform_application(
            PlatformApplicationArn=sns_service.platform_app_arn
        )

        existing_endpoint = None
        for endpoint in response["Endpoints"]:
            if endpoint["Attributes"].get("Token") == request.device_token:
                existing_endpoint = endpoint["EndpointArn"]
                break

        # Step 2: If endpoint exists, return it
        if existing_endpoint:
            return {
                "status": "already_registered",
                "endpoint_arn": existing_endpoint
            }

        # Step 3: If not, create a new one and subscribe it to the topic
        endpoint_arn = sns_service.create_platform_endpoint(
            fcm_token=request.device_token,
            user_id=request.user_id
        )

        try:
            subscription_arn = sns_service.subscribe_to_topic(endpoint_arn)
        except Exception as subscribe_error:
            return {
                "status": "error",
                "message": f"Endpoint created but subscription failed: {subscribe_error}",
                "endpoint_arn": endpoint_arn,
            }

        return {
            "status": "registered",
            "endpoint_arn": endpoint_arn,
            "subscription_arn": subscription_arn,
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }


# Note: Chat functionality has been moved to MirrorGPT routes at /api/mirrorgpt/chat
# This provides the full MirrorGPT experience with 5-signal analysis and archetype guidance

