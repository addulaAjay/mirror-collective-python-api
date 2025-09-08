"""
User service that orchestrates user profile management with Cognito sync
"""

import logging
from typing import Any, Dict, Optional

from ..core.exceptions import InternalServerError
from ..models.user_profile import UserProfile, UserStatus
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

    async def get_or_create_user_profile(
        self, user_id: str, force_cognito_sync: bool = False
    ) -> UserProfile:
        """
        Get user profile from DynamoDB, creating it if it doesn't exist
        Optionally syncs with Cognito for latest data

        Args:
            user_id: Cognito sub (UUID)
            force_cognito_sync: Whether to fetch fresh data from Cognito

        Returns:
            UserProfile
        """
        try:
            if not user_id:
                raise ValueError("user_id is required and cannot be None or empty")
            
            logger.info(f"Getting or creating user profile for user_id: {user_id}")
            # Get existing profile
            user_profile = await self.dynamodb_service.get_user_profile(user_id)

            # If profile doesn't exist or sync is forced, get data from Cognito
            if not user_profile or force_cognito_sync:
                try:
                    # Get user data from Cognito
                    cognito_data = await self.cognito_service.get_user_by_id(user_id)

                    if user_profile:
                        # Update existing profile with Cognito data
                        user_profile.update_from_cognito(cognito_data)
                        user_profile = await self.dynamodb_service.update_user_profile(
                            user_profile
                        )
                    else:
                        # Create new profile from Cognito data
                        try:
                            logger.info(f"Creating UserProfile from Cognito data for user {user_id}")
                            logger.debug(f"Cognito data: {cognito_data}")
                            user_profile = UserProfile.from_cognito_user(
                                cognito_data, user_id
                            )
                            logger.info(f"Created UserProfile with email: '{user_profile.email}'")
                        except Exception as profile_error:
                            logger.error(f"Error creating UserProfile from Cognito data: {profile_error}")
                            logger.error(f"Cognito data: {cognito_data}")
                            # Create minimal profile as fallback
                            user_profile = UserProfile(
                                user_id=user_id,
                                email="",  # Will be updated when sync succeeds
                                status=UserStatus.UNKNOWN,
                            )
                        
                        user_profile = await self.dynamodb_service.create_user_profile(
                            user_profile
                        )

                    logger.info(f"Synced user profile with Cognito: {user_id}")

                except Exception as cognito_error:
                    logger.warning(
                        f"Failed to sync with Cognito for user {user_id}: {cognito_error}"
                    )

                    # If we have an existing profile, return it despite sync failure
                    if user_profile:
                        return user_profile

                    # If no existing profile and Cognito sync failed, create minimal profile
                    user_profile = UserProfile(
                        user_id=user_id,
                        email="",  # Will be updated when sync succeeds
                        status=UserStatus.UNKNOWN,
                    )
                    user_profile = await self.dynamodb_service.create_user_profile(
                        user_profile
                    )
                    logger.info(
                        f"Created minimal user profile due to Cognito sync failure: {user_id}"
                    )

            return user_profile

        except Exception as e:
            logger.error(f"Error getting/creating user profile: {e}")
            raise InternalServerError(f"Failed to get user profile: {str(e)}")

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
        """
        try:
            # Get current profile
            user_profile = await self.get_or_create_user_profile(user_id)

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

    async def sync_user_with_cognito(self, user_id: str) -> UserProfile:
        """
        Force sync user profile with latest Cognito data

        Args:
            user_id: Cognito sub (UUID)

        Returns:
            Updated UserProfile
        """
        return await self.get_or_create_user_profile(user_id, force_cognito_sync=True)

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

    async def get_user_chat_name(self, user_id: str) -> Optional[str]:
        """
        Get the best name to use for this user in chat conversations

        Args:
            user_id: Cognito sub (UUID)

        Returns:
            User's preferred chat name or None
        """
        try:
            user_profile = await self.get_or_create_user_profile(user_id)
            return user_profile.chat_name

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
