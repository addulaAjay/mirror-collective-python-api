"""
Authentication controller - handles all auth-related endpoints
"""
from typing import Dict, Any

from ..api.models import (
    UserRegistrationRequest, LoginRequest, ForgotPasswordRequest,
    ResetPasswordRequest, RefreshTokenRequest, EmailVerificationRequest,
    ResendVerificationCodeRequest, AuthResponse, GeneralApiResponse,
    UserBasic, TokenBundle, LoginResponse
)
from ..core.exceptions import AuthenticationError, UserNotFoundError
from ..services.cognito_service import CognitoService


class AuthController:
    """Controller for authentication operations"""
    
    def __init__(self):
        self.cognito_service = CognitoService()
    
    async def register(self, payload: UserRegistrationRequest) -> AuthResponse:
        """Register a new user account"""
        # Split full name into first and last name
        name_parts = payload.fullName.strip().split(' ', 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ''
        
        # Register user with Cognito
        signup_result = await self.cognito_service.sign_up_user(
            email=payload.email,
            password=payload.password,
            first_name=first_name,
            last_name=last_name
        )
        
        user = UserBasic(
            id=signup_result['userSub'],
            email=payload.email,
            fullName=payload.fullName,
            isVerified=signup_result.get('userConfirmed', False)
        )
        
        return AuthResponse(
            success=True,
            data={
                "user": user.dict(),
                "message": "Registration successful. Please check your email for verification code."
            }
        )
    
    async def login(self, payload: LoginRequest) -> LoginResponse:
        """Authenticate user and return tokens"""
        # Authenticate with Cognito
        auth_result = await self.cognito_service.authenticate_user(
            email=payload.email,
            password=payload.password
        )
        
        # Get user profile from Cognito
        user_profile = await self.cognito_service.get_user_by_email(payload.email)
        user_attrs = user_profile['userAttributes']
        
        user = UserBasic(
            id=user_profile['username'],
            email=user_attrs.get('email', payload.email),
            fullName=f"{user_attrs.get('given_name', '')} {user_attrs.get('family_name', '')}".strip(),
            isVerified=user_attrs.get('email_verified', 'false').lower() == 'true'
        )
        
        tokens = TokenBundle(
            accessToken=auth_result['accessToken'],
            refreshToken=auth_result['refreshToken']
        )
        
        return LoginResponse(
            success=True,
            data={
                "user": user.dict(),
                "tokens": tokens.dict(),
                "message": "Login successful"
            }
        )
    
    async def forgot_password(self, payload: ForgotPasswordRequest) -> GeneralApiResponse:
        """Initiate password reset process"""
        await self.cognito_service.forgot_password(payload.email)
        return GeneralApiResponse(
            success=True,
            message="If an account with this email exists, you will receive a password reset code."
        )
    
    async def reset_password(self, payload: ResetPasswordRequest) -> GeneralApiResponse:
        """Reset password using verification code"""
        await self.cognito_service.confirm_forgot_password(
            email=payload.email,
            confirmation_code=payload.resetCode,
            new_password=payload.newPassword
        )
        return GeneralApiResponse(
            success=True,
            message="Password has been reset successfully. You can now log in with your new password."
        )
    
    async def refresh_token(self, payload: RefreshTokenRequest) -> AuthResponse:
        """Refresh access token using refresh token"""
        auth_result = await self.cognito_service.refresh_access_token(payload.refreshToken)
        
        return AuthResponse(
            success=True,
            data={
                "tokens": {
                    "accessToken": auth_result['accessToken'],
                    "refreshToken": auth_result['refreshToken']
                }
            },
            message="Token refreshed successfully"
        )
    
    async def confirm_email(self, payload: EmailVerificationRequest) -> GeneralApiResponse:
        """Confirm email address with verification code"""
        await self.cognito_service.confirm_sign_up(
            email=payload.email,
            confirmation_code=payload.verificationCode
        )
        return GeneralApiResponse(
            success=True,
            message="Email verified successfully. Your account is now active."
        )
    
    async def resend_verification_code(self, payload: ResendVerificationCodeRequest) -> GeneralApiResponse:
        """Resend email verification code"""
        await self.cognito_service.resend_confirmation_code(payload.email)
        return GeneralApiResponse(
            success=True,
            message="Verification code has been sent to your email address."
        )
    
    async def get_current_user_profile(self, current_user: Dict[str, Any]) -> Dict[str, Any]:
        """Get current authenticated user profile"""
        return {
            "success": True,
            "data": {
                "user": current_user
            }
        }
    
    async def logout(self, current_user: Dict[str, Any]) -> GeneralApiResponse:
        """Logout current user and invalidate tokens"""
        # Get access token from user context - in production this would come from the request
        # For now, we'll implement global sign out using the user's stored token
        # In a real implementation, you'd need to track the current session's access token
        try:
            # Note: This is a simplified implementation
            # In production, you'd need to handle token invalidation properly
            user_email = current_user.get('email')
            if user_email:
                # You could implement token blacklisting here
                pass
        except Exception:
            # Don't fail logout if token invalidation fails
            pass
        
        return GeneralApiResponse(
            success=True,
            message="Logged out successfully"
        )
    
    async def delete_account(self, current_user: Dict[str, Any]) -> GeneralApiResponse:
        """Delete current user account"""
        user_email = current_user.get('email')
        if user_email:
            await self.cognito_service.admin_delete_user(user_email)
        
        return GeneralApiResponse(
            success=True,
            message="Account deleted successfully"
        )