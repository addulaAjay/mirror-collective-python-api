import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from jose import jwt

from ..controllers.auth_controller import AuthController
from ..core.enhanced_auth import get_user_with_profile
from ..core.security import get_current_user
from ..services.dynamodb_service import DynamoDBService
from ..services.sns_service import SNSService
from .models import (
    AuthResponse,
    DeviceRegistrationRequest,
    DeviceUnregistrationRequest,
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
dynamodb_service = DynamoDBService()


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
async def send_notification(
    request: NotificationRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Admin/Dev endpoint to send broadcast notifications (Requires Auth)"""
    logger.info(f"User {current_user['id']} triggered a broadcast notification")
    msg_id = sns_service.publish_to_topic(request.title, request.body)
    return {
        "success": True,
        "data": {"message_id": msg_id},
        "message": "Notification broadcast initiated",
    }


# device registration endpoint
@router.post("/register-device")
async def register_device(
    request: DeviceRegistrationRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Register a device for push notifications (Requires Auth)"""
    user_id = current_user["id"]
    device_token = request.device_token
    platform = request.platform

    try:
        # Step 1: Check if we already have this registration in DB
        existing_registration = await dynamodb_service.get_device_token(
            user_id, device_token
        )

        if existing_registration:
            logger.info(
                f"Device already registered for user {user_id}. Returning existing ARN."
            )
            return {
                "success": True,
                "data": {
                    "status": "already_registered",
                    "endpoint_arn": existing_registration["endpoint_arn"],
                },
            }

        # Step 2: Create new endpoint in SNS
        endpoint_arn = sns_service.create_platform_endpoint(
            token=device_token, platform=platform, user_id=user_id
        )

        # Step 3: Subscribe to the main topic
        try:
            subscription_arn = sns_service.subscribe_to_topic(endpoint_arn)
        except Exception as subscribe_error:
            logger.error(f"Endpoint created but subscription failed: {subscribe_error}")
            subscription_arn = None

        # Step 4: Persist in DynamoDB for O(1) lookups in the future
        # First, cleanup any existing 'GUEST' entry for this device
        await dynamodb_service.cleanup_guest_registration(device_token)

        saved = await dynamodb_service.save_device_token(
            user_id=user_id,
            device_token=device_token,
            endpoint_arn=endpoint_arn,
            platform=platform,
        )

        if not saved:
            logger.warning(
                f"Registration succeeded but DB persistence failed for user {user_id}"
            )

        return {
            "success": True,
            "data": {
                "status": "registered",
                "endpoint_arn": endpoint_arn,
                "subscription_arn": subscription_arn,
            },
            "message": "Device registered successfully",
        }

    except Exception as e:
        logger.error(f"Error in register_device: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/unregister-device")
async def unregister_device(
    request: DeviceUnregistrationRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """De-identify a device on logout (keep in DB as GUEST, do not delete SNS)"""
    user_id = current_user["id"]
    device_token = request.device_token

    try:
        # Step 1: De-identify the device in DynamoDB
        # This clears the user_id link but keeps the record (as GUEST) for retention broadcasts.
        # We do NOT delete the SNS endpoint to allow for future re-engagement.
        success = await dynamodb_service.deidentify_device_token(user_id, device_token)

        if success:
            return {"success": True, "message": "Device de-identified successfully"}
        else:
            return {"success": False, "message": "Failed to de-identify device"}
    except Exception as e:
        logger.error(f"Error in unregister_device: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Note: Chat functionality has been moved to MirrorGPT routes at /api/mirrorgpt/chat
# This provides the full MirrorGPT experience with 5-signal analysis and archetype guidance
