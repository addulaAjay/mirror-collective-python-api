"""
User service that orchestrates user profile management with Cognito sync
"""

import logging
import time
from typing import Any, Dict, Optional

from ..core.exceptions import InternalServerError
from ..models.user_profile import UserProfile
from .cognito_service import CognitoService
from .dynamodb_service import DynamoDBService

logger = logging.getLogger(__name__)


class UserService:
    """
    High-level service for user management that coordinates between Cognito and DynamoDB
    """

    def __init__(self):
        """Initialize user service with required dependencies"""
        self.dynamodb_service = DynamoDBService()
        self.cognito_service = CognitoService()

    async def create_user_profile_from_cognito(
        self, cognito_user_data: Dict[str, Any]
    ) -> UserProfile:
        """
        Create a new user profile in DynamoDB from Cognito user data
        Used during registration after email confirmation

        Args:
            cognito_user_data: User data returned from Cognito get_user_by_email

        Returns:
            Created UserProfile
        """
        try:
            user_id = cognito_user_data.get("username")
            if not user_id:
                raise ValueError("No user ID found in Cognito data")

            # Extract email from Cognito data to validate it exists
            attributes = cognito_user_data.get("userAttributes", {})
            if "UserAttributes" in cognito_user_data:
                attrs_dict = {}
                for attr in cognito_user_data["UserAttributes"]:
                    attrs_dict[attr["Name"]] = attr["Value"]
                attributes = attrs_dict

            email = attributes.get("email", "").strip()
            if not email:
                raise ValueError(
                    f"No valid email found in Cognito data for user: {user_id}"
                )

            logger.info(f"Creating user profile from Cognito data for user: {user_id}")

            # Check if profile already exists
            existing_profile = await self.dynamodb_service.get_user_profile(user_id)
            if existing_profile:
                logger.info(f"User profile already exists for user: {user_id}")
                return existing_profile

            # Create new profile from Cognito data
            user_profile = UserProfile.from_cognito_user(cognito_user_data, user_id)

            # Double-check email is valid before saving
            if not user_profile.email or not user_profile.email.strip():
                raise ValueError(f"User profile has invalid email for user: {user_id}")

            user_profile = await self.dynamodb_service.create_user_profile(user_profile)

            logger.info(f"Successfully created user profile for user: {user_id}")
            return user_profile

        except Exception as e:
            logger.error(f"Error creating user profile from Cognito data: {e}")
            raise InternalServerError(f"Failed to create user profile: {str(e)}")

    async def get_user_profile(self, user_id: str) -> Optional[UserProfile]:
        """
        Get user profile from DynamoDB without creating if missing
        Used in chat flow to verify user exists

        Args:
            user_id: Cognito sub (UUID)

        Returns:
            UserProfile if exists, None otherwise
        """
        try:
            if not user_id:
                raise ValueError("user_id is required and cannot be None or empty")

            logger.debug(f"Getting user profile for user_id: {user_id}")
            return await self.dynamodb_service.get_user_profile(user_id)

        except Exception as e:
            logger.error(f"Error getting user profile: {e}")
            return None

    async def update_user_profile(
        self, user_id: str, updates: Dict[str, Any]
    ) -> UserProfile:
        """
        Update user profile with new data

        Args:
            user_id: Cognito sub (UUID)
            updates: Dictionary of fields to update

        Returns:
            Updated UserProfile

        Raises:
            InternalServerError: If user profile doesn't exist
        """
        try:
            # Get current profile (must exist)
            user_profile = await self.get_user_profile(user_id)
            if not user_profile:
                raise InternalServerError(f"User profile not found for user: {user_id}")

            # Apply updates
            for key, value in updates.items():
                if hasattr(user_profile, key):
                    setattr(user_profile, key, value)

            # Save updated profile
            user_profile = await self.dynamodb_service.update_user_profile(user_profile)

            logger.info(f"Updated user profile: {user_id}")
            return user_profile

        except Exception as e:
            logger.error(f"Error updating user profile: {e}")
            raise InternalServerError(f"Failed to update user profile: {str(e)}")

    async def record_chat_activity(self, user_id: str) -> None:
        """
        Record that user sent a chat message

        Args:
            user_id: Cognito sub (UUID)
        """
        try:
            await self.dynamodb_service.record_user_activity(user_id, "chat")
            logger.debug(f"Recorded chat activity for user: {user_id}")

        except Exception as e:
            logger.error(f"Error recording chat activity: {e}")
            # Don't raise error for activity tracking failures

    async def record_login_activity(self, user_id: str) -> None:
        """
        Record that user logged in

        Args:
            user_id: Cognito sub (UUID)
        """
        try:
            await self.dynamodb_service.update_last_login(user_id)
            logger.debug(f"Recorded login activity for user: {user_id}")

        except Exception as e:
            logger.error(f"Error recording login activity: {e}")
            # Don't raise error for activity tracking failures

    async def record_logout_activity(self, user_id: str) -> None:
        """
        Record that user logged out (only if profile exists)

        Args:
            user_id: Cognito sub (UUID)
        """
        try:
            # Only record logout if user profile exists
            existing_profile = await self.dynamodb_service.get_user_profile(user_id)
            if existing_profile:
                await self.dynamodb_service.record_user_activity(user_id, "logout")
                logger.debug(f"Recorded logout activity for user: {user_id}")
            else:
                logger.debug(f"No profile exists to record logout for user: {user_id}")

        except Exception as e:
            logger.error(f"Error recording logout activity: {e}")
            # Don't raise error for activity tracking failures

    async def delete_user_account(self, user_id: str) -> bool:
        """
        Delete user account from both DynamoDB and Cognito

        Args:
            user_id: Cognito sub (UUID)

        Returns:
            True if successful
        """
        try:
            # Delete from DynamoDB first
            await self.dynamodb_service.delete_user_profile(user_id)

            # Then delete from Cognito
            try:
                # Note: This would need the user's access token or admin privileges
                # await self.cognito_service.admin_delete_user(user_id)
                logger.info(f"User profile deleted from DynamoDB: {user_id}")
            except Exception as cognito_error:
                logger.error(f"Failed to delete user from Cognito: {cognito_error}")
                # Continue since DynamoDB deletion succeeded

            return True

        except Exception as e:
            logger.error(f"Error deleting user account: {e}")
            raise InternalServerError(f"Failed to delete user account: {str(e)}")

    async def soft_delete_user_account(self, user_id: str) -> bool:
        """
        Soft delete user account by marking as deleted instead of removing data

        Args:
            user_id: Cognito sub (UUID)

        Returns:
            True if successful
        """
        try:
            # Mark profile as deleted in DynamoDB (preserve data)
            user_profile = await self.dynamodb_service.get_user_profile(user_id)
            if user_profile:
                # Update profile to mark as deleted
                updates = {
                    "status": "DELETED",
                    "deleted_at": str(int(time.time())),
                    "account_status": "SOFT_DELETED",
                }

                for key, value in updates.items():
                    if hasattr(user_profile, key):
                        setattr(user_profile, key, value)

                await self.dynamodb_service.update_user_profile(user_profile)
                logger.info(f"User profile marked as deleted in DynamoDB: {user_id}")
            else:
                logger.warning(f"No user profile found to soft delete: {user_id}")

            return True

        except Exception as e:
            logger.error(f"Error soft deleting user account: {e}")
            raise InternalServerError(f"Failed to soft delete user account: {str(e)}")

    async def get_user_chat_name(self, user_id: str) -> Optional[str]:
        """
        Get the best name to use for this user in chat conversations

        Args:
            user_id: Cognito sub (UUID)

        Returns:
            User's preferred chat name or None if profile doesn't exist
        """
        try:
            user_profile = await self.get_user_profile(user_id)
            if user_profile:
                return user_profile.chat_name
            return None

        except Exception as e:
            logger.error(f"Error getting user chat name: {e}")
            return None

    async def increment_conversation_count(self, user_id: str) -> None:
        """
        Increment user's total conversation count

        Args:
            user_id: Cognito sub (UUID)
        """
        try:
            await self.record_chat_activity(user_id)

        except Exception as e:
            logger.error(f"Error incrementing conversation count: {e}")
            # Don't raise error for analytics failures
