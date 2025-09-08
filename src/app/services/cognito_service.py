"""
AWS Cognito service for user authentication and management
"""

import base64
import hashlib
import hmac
import logging
import os
from typing import Any, Dict, NoReturn, Optional

import boto3
from botocore.exceptions import ClientError

from ..core.exceptions import (
    AuthenticationError,
    CognitoServiceError,
    UserAlreadyExistsError,
    UserNotFoundError,
    ValidationError,
)

logger = logging.getLogger(__name__)


class CognitoService:
    """Service for AWS Cognito user pool operations"""

    def __init__(self):
        self.region = os.getenv("AWS_REGION", "us-east-1")
        user_pool_id = os.getenv("COGNITO_USER_POOL_ID")
        client_id = os.getenv("COGNITO_CLIENT_ID")
        self.client_secret = os.getenv("COGNITO_CLIENT_SECRET")

        if not all([user_pool_id, client_id]):
            raise ValueError("Missing required Cognito configuration")

        # Runtime checks instead of assert statements for security
        if user_pool_id is None:
            raise ValueError("COGNITO_USER_POOL_ID environment variable is required")
        if client_id is None:
            raise ValueError("COGNITO_CLIENT_ID environment variable is required")

        self.user_pool_id: str = user_pool_id
        self.client_id: str = client_id

        self.client = boto3.client("cognito-idp", region_name=self.region)

        logger.info(f"Initialized CognitoService for pool {self.user_pool_id}")

    def _get_secret_hash(self, username: str) -> Optional[str]:
        """Generate secret hash for Cognito client"""
        if not self.client_secret:
            return None

        message = username + self.client_id
        dig = hmac.new(
            str(self.client_secret).encode("utf-8"),
            msg=str(message).encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        return base64.b64encode(dig).decode()

    def _handle_cognito_error(self, error: ClientError, operation: str) -> NoReturn:
        """Handle and transform Cognito errors to our custom exceptions"""
        error_code = error.response["Error"]["Code"]
        error_message = error.response["Error"]["Message"]

        logger.error(f"Cognito {operation} error: {error_code} - {error_message}")

        error_mappings = {
            "UsernameExistsException": UserAlreadyExistsError(
                "An account with this email already exists"
            ),
            "UserNotFoundException": UserNotFoundError("User not found"),
            "NotAuthorizedException": AuthenticationError("Invalid email or password"),
            "InvalidPasswordException": ValidationError(
                "Password does not meet requirements",
                [{"field": "password", "message": error_message}],
            ),
            "InvalidParameterException": ValidationError(
                "Invalid parameters provided",
                [{"field": "general", "message": error_message}],
            ),
            "CodeMismatchException": ValidationError(
                "Invalid verification code",
                [{"field": "verificationCode", "message": "Invalid verification code"}],
            ),
            "ExpiredCodeException": ValidationError(
                "Verification code has expired",
                [
                    {
                        "field": "verificationCode",
                        "message": "Verification code has expired",
                    }
                ],
            ),
            "LimitExceededException": ValidationError(
                "Too many attempts. Please try again later.",
                [{"field": "general", "message": "Rate limit exceeded"}],
            ),
            "TooManyRequestsException": ValidationError(
                "Too many requests. Please try again later.",
                [{"field": "general", "message": "Rate limit exceeded"}],
            ),
            "UserNotConfirmedException": AuthenticationError(
                "Account not verified. Please check your email for verification code."
            ),
            "PasswordResetRequiredException": AuthenticationError(
                "Password reset required. Please reset your password."
            ),
        }

        if error_code in error_mappings:
            raise error_mappings[error_code]
        else:
            raise CognitoServiceError(
                f"Cognito operation failed: {error_message}", error_code
            )

    async def sign_up_user(
        self, email: str, password: str, first_name: str, last_name: str
    ) -> Dict[str, Any]:
        """Register a new user in Cognito using SignUpCommand (self-registration)"""
        try:
            params = {
                "ClientId": self.client_id,
                "Username": email,  # Use email as username for self-registration
                "Password": password,
                "UserAttributes": [
                    {"Name": "email", "Value": email},
                    {"Name": "given_name", "Value": first_name},
                    {"Name": "family_name", "Value": last_name},
                ],
            }

            if self.client_secret:
                secret_hash = self._get_secret_hash(email)
                if secret_hash:
                    params["SecretHash"] = secret_hash

            response = self.client.sign_up(**params)

            logger.info(f"User registered successfully: {email}")

            return {
                "userSub": response["UserSub"],
                "codeDeliveryDetails": response.get("CodeDeliveryDetails", {}),
                "userConfirmed": response.get("UserConfirmed", False),
            }

        except ClientError as e:
            self._handle_cognito_error(e, "sign_up")
        except Exception as e:
            logger.exception(f"Unexpected error during sign up: {str(e)}")
            raise CognitoServiceError(f"Sign up failed: {str(e)}")

    async def authenticate_user(self, email: str, password: str) -> Dict[str, Any]:
        """Authenticate user with email and password using ADMIN_NO_SRP_AUTH"""
        try:
            params: Dict[str, Any] = {
                "UserPoolId": self.user_pool_id,
                "ClientId": self.client_id,
                "AuthFlow": "ADMIN_NO_SRP_AUTH",
                "AuthParameters": {"USERNAME": email, "PASSWORD": password},
            }

            if self.client_secret:
                secret_hash = self._get_secret_hash(email)
                if secret_hash:
                    params["AuthParameters"]["SECRET_HASH"] = secret_hash

            response = self.client.admin_initiate_auth(**params)

            if not response.get("AuthenticationResult"):
                raise AuthenticationError("Authentication failed - no tokens returned")

            auth_result = response["AuthenticationResult"]
            access_token = auth_result.get("AccessToken")
            refresh_token = auth_result.get("RefreshToken")
            id_token = auth_result.get("IdToken")

            if not all([access_token, refresh_token, id_token]):
                raise AuthenticationError(
                    "Authentication failed - incomplete token response"
                )

            logger.info(f"User authenticated successfully: {email}")

            return {
                "accessToken": access_token,
                "refreshToken": refresh_token,
                "idToken": id_token,
            }

        except ClientError as e:
            self._handle_cognito_error(e, "authentication")
        except Exception as e:
            logger.exception(f"Unexpected error during authentication: {str(e)}")
            raise CognitoServiceError(f"Authentication failed: {str(e)}")

    async def forgot_password(self, email: str) -> Dict[str, Any]:
        """Initiate password reset flow"""
        try:
            params = {"ClientId": self.client_id, "Username": email}

            if self.client_secret:
                secret_hash = self._get_secret_hash(email)
                if secret_hash:
                    params["SecretHash"] = secret_hash

            response = self.client.forgot_password(**params)

            logger.info(f"Password reset initiated for: {email}")

            return response

        except ClientError as e:
            self._handle_cognito_error(e, "forgot_password")
        except Exception as e:
            logger.exception(f"Unexpected error during forgot password: {str(e)}")
            raise CognitoServiceError(f"Forgot password failed: {str(e)}")

    async def confirm_forgot_password(
        self, email: str, confirmation_code: str, new_password: str
    ) -> Dict[str, Any]:
        """Confirm password reset with verification code"""
        try:
            params = {
                "ClientId": self.client_id,
                "Username": email,
                "ConfirmationCode": confirmation_code,
                "Password": new_password,
            }

            if self.client_secret:
                secret_hash = self._get_secret_hash(email)
                if secret_hash:
                    params["SecretHash"] = secret_hash

            response = self.client.confirm_forgot_password(**params)

            logger.info(f"Password reset confirmed for: {email}")

            return response

        except ClientError as e:
            self._handle_cognito_error(e, "confirm_forgot_password")
        except Exception as e:
            logger.exception(
                f"Unexpected error during password reset confirmation: {str(e)}"
            )
            raise CognitoServiceError(f"Password reset confirmation failed: {str(e)}")

    async def confirm_sign_up(
        self, email: str, confirmation_code: str
    ) -> Dict[str, Any]:
        """Confirm user email with verification code"""
        try:
            params = {
                "ClientId": self.client_id,
                "Username": email,
                "ConfirmationCode": confirmation_code,
            }

            if self.client_secret:
                secret_hash = self._get_secret_hash(email)
                if secret_hash:
                    params["SecretHash"] = secret_hash

            response = self.client.confirm_sign_up(**params)

            logger.info(f"Email confirmed for: {email}")

            return response

        except ClientError as e:
            self._handle_cognito_error(e, "confirm_sign_up")
        except Exception as e:
            logger.exception(f"Unexpected error during email confirmation: {str(e)}")
            raise CognitoServiceError(f"Email confirmation failed: {str(e)}")

    async def resend_confirmation_code(self, email: str) -> Dict[str, Any]:
        """Resend email verification code"""
        try:
            params = {"ClientId": self.client_id, "Username": email}

            if self.client_secret:
                secret_hash = self._get_secret_hash(email)
                if secret_hash:
                    params["SecretHash"] = secret_hash

            response = self.client.resend_confirmation_code(**params)

            logger.info(f"Confirmation code resent for: {email}")

            return response

        except ClientError as e:
            self._handle_cognito_error(e, "resend_confirmation_code")
        except Exception as e:
            logger.exception(f"Unexpected error during resend confirmation: {str(e)}")
            raise CognitoServiceError(f"Resend confirmation failed: {str(e)}")

    async def refresh_access_token(self, refresh_token: str) -> Dict[str, Any]:
        """Refresh access token using refresh token"""
        try:
            params: Dict[str, Any] = {
                "ClientId": self.client_id,
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "AuthParameters": {"REFRESH_TOKEN": refresh_token},
            }

            # For refresh token flow with client secret, use empty username for SECRET_HASH
            # This matches the Node.js implementation behavior
            if self.client_secret:
                secret_hash = self._get_secret_hash("")  # Empty username for refresh
                if secret_hash:
                    params["AuthParameters"]["SECRET_HASH"] = secret_hash

            response = self.client.initiate_auth(**params)

            if not response.get("AuthenticationResult"):
                raise AuthenticationError("Token refresh failed")

            auth_result = response["AuthenticationResult"]
            access_token = auth_result.get("AccessToken")
            new_refresh_token = auth_result.get("RefreshToken")
            id_token = auth_result.get("IdToken")

            if not access_token or not id_token:
                raise AuthenticationError("Token refresh returned incomplete result")

            logger.info("Token refreshed successfully")

            return {
                "accessToken": access_token,
                "refreshToken": new_refresh_token
                or refresh_token,  # Use new if provided, otherwise keep old
                "idToken": id_token,
            }

        except ClientError as e:
            # Custom error handling for refresh token operations
            error_code = e.response["Error"]["Code"]
            error_message = e.response["Error"]["Message"]
            
            logger.error(f"Cognito refresh_token error: {error_code} - {error_message}")
            
            # Specific error mappings for refresh token flow
            if error_code == "NotAuthorizedException":
                raise AuthenticationError("Invalid or expired refresh token")
            elif error_code == "InvalidParameterException":
                raise ValidationError("Invalid refresh token format")
            else:
                # Fall back to general error handling
                self._handle_cognito_error(e, "refresh_token")
        except Exception as e:
            logger.exception(f"Unexpected error during token refresh: {str(e)}")
            raise CognitoServiceError(f"Token refresh failed: {str(e)}")

    async def get_user(self, access_token: str) -> Dict[str, Any]:
        """Get user details using access token"""
        try:
            response = self.client.get_user(AccessToken=access_token)

            # Convert user attributes to a more usable format
            user_attributes = {}
            for attr in response.get("UserAttributes", []):
                user_attributes[attr["Name"]] = attr["Value"]

            return {
                "username": response["Username"],
                "userAttributes": user_attributes,
                "enabled": response.get("Enabled", True),
                "userStatus": response.get("UserStatus", "UNKNOWN"),
            }

        except ClientError as e:
            self._handle_cognito_error(e, "get_user")
        except Exception as e:
            logger.exception(f"Unexpected error during get user: {str(e)}")
            raise CognitoServiceError(f"Get user failed: {str(e)}")

    async def delete_user(self, access_token: str) -> Dict[str, Any]:
        """Delete user account using access token"""
        try:
            response = self.client.delete_user(AccessToken=access_token)

            logger.info("User account deleted successfully")

            return response

        except ClientError as e:
            self._handle_cognito_error(e, "delete_user")
        except Exception as e:
            logger.exception(f"Unexpected error during account deletion: {str(e)}")
            raise CognitoServiceError(f"Account deletion failed: {str(e)}")

    async def admin_delete_user(self, email: str) -> Dict[str, Any]:
        """Delete user account using admin privileges"""
        try:
            response = self.client.admin_delete_user(
                UserPoolId=self.user_pool_id, Username=email
            )

            logger.info(f"User account deleted successfully: {email}")

            return response

        except ClientError as e:
            self._handle_cognito_error(e, "admin_delete_user")
        except Exception as e:
            logger.exception(
                f"Unexpected error during admin account deletion: {str(e)}"
            )
            raise CognitoServiceError(f"Admin account deletion failed: {str(e)}")

    async def get_user_by_email(self, email: str) -> Dict[str, Any]:
        """Get user details using admin privileges"""
        try:
            response = self.client.admin_get_user(
                UserPoolId=self.user_pool_id, Username=email
            )

            # Convert user attributes to a more usable format
            user_attributes = {}
            for attr in response.get("UserAttributes", []):
                user_attributes[attr["Name"]] = attr["Value"]

            return {
                "username": response["Username"],
                "userAttributes": user_attributes,
                "enabled": response.get("Enabled", True),
                "userStatus": response.get("UserStatus", "UNKNOWN"),
                "userCreateDate": response.get("UserCreateDate"),
                "userLastModifiedDate": response.get("UserLastModifiedDate"),
            }

        except ClientError as e:
            self._handle_cognito_error(e, "get_user_by_email")
        except Exception as e:
            logger.exception(f"Unexpected error during get user by email: {str(e)}")
            raise CognitoServiceError(f"Get user by email failed: {str(e)}")

    async def global_sign_out(self, access_token: str) -> Dict[str, Any]:
        """Sign out user from all devices"""
        try:
            response = self.client.global_sign_out(AccessToken=access_token)

            logger.info("User signed out globally")

            return response

        except ClientError as e:
            self._handle_cognito_error(e, "global_sign_out")
        except Exception as e:
            logger.exception(f"Unexpected error during global sign out: {str(e)}")
            raise CognitoServiceError(f"Global sign out failed: {str(e)}")

    async def get_user_by_id(self, user_id: str) -> Dict[str, Any]:
        """Get user details by Cognito user ID (sub)"""
        try:
            # Note: This requires admin privileges and would need to be implemented
            # For now, we'll use the email-based lookup or return mock data
            # In production, you'd use admin_get_user with proper permissions

            # Placeholder implementation - would need proper admin setup
            logger.warning(f"get_user_by_id not fully implemented for user: {user_id}")
            return {
                "Username": user_id,
                "UserStatus": "CONFIRMED",
                "UserAttributes": [
                    {"Name": "sub", "Value": user_id},
                    {"Name": "email", "Value": ""},
                    {"Name": "email_verified", "Value": "false"},
                ],
            }

        except Exception as e:
            logger.exception(f"Unexpected error getting user by ID: {str(e)}")
            raise CognitoServiceError(f"Get user by ID failed: {str(e)}")
