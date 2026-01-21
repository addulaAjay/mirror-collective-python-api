"""
Authentication controller - handles all auth-related endpoints
"""

import logging
from typing import Any, Dict

from ..api.models import (
    AuthResponse,
    EmailVerificationRequest,
    ForgotPasswordRequest,
    GeneralApiResponse,
    LoginRequest,
    LoginResponse,
    RefreshTokenRequest,
    ResendVerificationCodeRequest,
    ResetPasswordRequest,
    TokenBundle,
    UserBasic,
    UserRegistrationRequest,
)
from ..services.cognito_service import CognitoService
from ..services.dynamodb_service import DynamoDBService
from ..services.user_linking_service import UserLinkingService
from ..services.user_service import UserService

logger = logging.getLogger(__name__)


class AuthController:
    """Controller for authentication operations"""

    def __init__(self):
        self.cognito_service = CognitoService()
        self.user_service = UserService()
        dynamodb_service = DynamoDBService()
        self.linking_service = UserLinkingService(dynamodb_service)

    async def register(self, payload: UserRegistrationRequest) -> AuthResponse:
        """Register a new user account"""
        # Split full name into first and last name
        name_parts = payload.fullName.strip().split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        # Register user with Cognito
        signup_result = await self.cognito_service.sign_up_user(
            email=payload.email,
            password=payload.password,
            first_name=first_name,
            last_name=last_name,
        )

        # Store anonymousId temporarily for linking after email verification
        # (We'll retrieve it from the registration request during confirm_email)
        if payload.anonymousId:
            # TODO: Store in cache/session for email confirmation flow
            logger.info(f"Registration with anonymousId: {payload.anonymousId}")

        user = UserBasic(
            id=signup_result["userSub"],
            email=payload.email,
            fullName=payload.fullName,
            isVerified=signup_result.get("userConfirmed", False),
        )

        return AuthResponse(
            success=True,
            data={
                "user": user.dict(),
                "message": (
                    "Registration successful. "
                    "Please check your email for verification code."
                ),
            },
        )

    async def login(self, payload: LoginRequest) -> LoginResponse:
        """Authenticate user and return tokens"""
        # Authenticate with Cognito
        auth_result = await self.cognito_service.authenticate_user(
            email=payload.email, password=payload.password
        )

        # Get user profile from Cognito
        user_profile = await self.cognito_service.get_user_by_email(payload.email)
        user_attrs = user_profile["userAttributes"]
        user_id = user_profile["username"]

        user = UserBasic(
            id=user_id,
            email=user_attrs.get("email", payload.email),
            fullName=f"{user_attrs.get('given_name', '')} "
            f"{user_attrs.get('family_name', '')}".strip(),
            isVerified=user_attrs.get("email_verified", "false").lower() == "true",
        )

        # Just record login activity - don't create profiles during login
        try:
            await self.user_service.record_login_activity(user_id)
            logger.info(f"Recorded login activity for user: {user_id}")

        except Exception as e:
            # Log the error but don't fail the login
            logger.error(f"Failed to record login activity: {e}")

        tokens = TokenBundle(
            accessToken=auth_result["accessToken"],
            refreshToken=auth_result["refreshToken"],
        )

        return LoginResponse(
            success=True,
            data={
                "user": user.dict(),
                "tokens": tokens.dict(),
                "message": "Login successful",
            },
        )

    async def forgot_password(
        self, payload: ForgotPasswordRequest
    ) -> GeneralApiResponse:
        """Initiate password reset process"""
        await self.cognito_service.forgot_password(payload.email)
        return GeneralApiResponse(
            success=True,
            message=(
                "If an account with this email exists, "
                "you will receive a password reset code."
            ),
        )

    async def reset_password(self, payload: ResetPasswordRequest) -> GeneralApiResponse:
        """Reset password using verification code"""
        # Reset password in Cognito
        await self.cognito_service.confirm_forgot_password(
            email=payload.email,
            confirmation_code=payload.resetCode,
            new_password=payload.newPassword,
        )

        # Update user profile with latest Cognito data (if profile exists)
        try:
            # Get updated user details from Cognito
            user_details = await self.cognito_service.get_user_by_email(payload.email)
            user_id = user_details["username"]

            # Only sync if profile already exists - don't create new ones
            existing_profile = await self.user_service.get_user_profile(user_id)
            if existing_profile:
                # Note: Cognito sync removed for security reasons
                logger.info(
                    f"User profile exists but sync with Cognito "
                    f"not available: {user_id}"
                )
            else:
                logger.info(f"No existing profile to sync for user: {user_id}")

        except Exception as e:
            # Log the error but don't fail the password reset
            logger.error(f"Failed to sync user data after password reset: {e}")

        return GeneralApiResponse(
            success=True,
            message=(
                "Password has been reset successfully. "
                "You can now log in with your new password."
            ),
        )

    async def refresh_token(self, payload: RefreshTokenRequest) -> AuthResponse:
        """Refresh access token using refresh token"""
        # Refresh token with Cognito
        auth_result = await self.cognito_service.refresh_access_token(
            payload.refreshToken
        )

        return AuthResponse(
            success=True,
            data={
                "tokens": {
                    "accessToken": auth_result["accessToken"],
                    "refreshToken": auth_result["refreshToken"],
                }
            },
            message="Token refreshed successfully",
        )

    async def confirm_email(
        self, payload: EmailVerificationRequest
    ) -> GeneralApiResponse:
        """
        Confirm email address with verification code
        and create user profile in DynamoDB
        """
        try:
            # Confirm email with Cognito
            await self.cognito_service.confirm_sign_up(
                email=payload.email, confirmation_code=payload.verificationCode
            )

            # After successful email confirmation, create user profile in DynamoDB
            try:
                # Get the full user details from Cognito
                user_details = await self.cognito_service.get_user_by_email(
                    payload.email
                )

                # Create user profile in DynamoDB
                user_profile = await self.user_service.create_user_profile_from_cognito(
                    user_details
                )
                logger.info(
                    f"Created user profile in DynamoDB for user: {user_profile.user_id}"
                )

                # Link anonymous quiz data if anonymousId was provided
                if payload.anonymousId:
                    try:
                        anon_id = f"anon_{payload.anonymousId}"
                        link_results = await self.linking_service.link_anonymous_data(
                            anonymous_id=anon_id, user_id=user_profile.user_id
                        )
                        logger.info(
                            f"Anonymous data linking completed for "
                            f"{user_profile.user_id}: {link_results}"
                        )
                    except Exception as link_error:
                        logger.error(f"Failed to link anonymous data: {link_error}")
                        # Don't fail email confirmation if linking fails

            except Exception as e:
                # Log the error but don't fail the email confirmation
                # The user can still use the system, profile creation can be retried
                logger.error(
                    f"Failed to create user profile in DynamoDB "
                    f"after email confirmation: {e}"
                )
                # In production, you might want to trigger a retry mechanism or alert

            return GeneralApiResponse(
                success=True,
                message="Email verified successfully. Your account is now active.",
            )

        except Exception as e:
            logger.error(f"Email confirmation failed: {e}")
            raise

    async def resend_verification_code(
        self, payload: ResendVerificationCodeRequest
    ) -> GeneralApiResponse:
        """Resend email verification code"""
        await self.cognito_service.resend_confirmation_code(payload.email)
        return GeneralApiResponse(
            success=True,
            message="Verification code has been sent to your email address.",
        )

    async def get_current_user_profile(
        self, current_user: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Get current authenticated user profile"""
        return {"success": True, "data": {"user": current_user}}

    async def logout(self, current_user: Dict[str, Any]) -> GeneralApiResponse:
        """Logout current user and invalidate tokens"""
        try:
            user_id = current_user.get("id") or current_user.get("sub")
            user_email = current_user.get("email")

            # Record logout activity (only if profile exists)
            if user_id:
                try:
                    await self.user_service.record_logout_activity(user_id)
                    logger.info(f"Logout activity recorded for user: {user_id}")
                except Exception as e:
                    logger.warning(f"Failed to record logout activity: {e}")

            # Invalidate tokens by signing out user from all devices
            if user_email:
                try:
                    await self.cognito_service.admin_user_global_sign_out(user_email)
                    logger.info(f"User signed out globally: {user_email}")
                except Exception as e:
                    # Don't fail logout if global sign out fails
                    logger.warning(f"Failed to sign out user globally: {e}")

            logger.info(f"User logout processed successfully: {user_id}")

        except Exception as e:
            # Don't fail logout if any step fails
            logger.warning(f"Error during logout processing: {e}")

        return GeneralApiResponse(success=True, message="Logged out successfully")

    async def delete_account(self, current_user: Dict[str, Any]) -> GeneralApiResponse:
        """Delete current user account from both Cognito and DynamoDB"""
        user_email = current_user.get("email")
        user_id = current_user.get("id") or current_user.get("sub")

        if user_email and user_id:
            try:
                await self.cognito_service.admin_delete_user(user_email)
                logger.info(f"Deleted user from Cognito: {user_email}")

                # Then delete from DynamoDB
                await self.user_service.delete_user_account(user_id)
                logger.info(f"Deleted user profile from DynamoDB: {user_id}")

            except Exception as e:
                logger.error(f"Error during account deletion: {e}")
                # Re-raise to ensure the client knows deletion failed
                raise

        return GeneralApiResponse(success=True, message="Account deleted successfully")
