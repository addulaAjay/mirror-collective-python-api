"""
User Linking Service - Links anonymous user data to authenticated accounts
Handles migration of quiz results and archetype profiles from anonymous IDs to real user IDs
"""

import logging
from typing import Optional

from ..services.dynamodb_service import DynamoDBService

logger = logging.getLogger(__name__)


class UserLinkingService:
    """Service to link anonymous user data to authenticated user accounts"""

    def __init__(self, dynamodb_service: DynamoDBService):
        self.dynamodb = dynamodb_service

    async def link_anonymous_data(
        self, anonymous_id: str, user_id: str
    ) -> dict[str, bool]:
        """
        Link all anonymous data to authenticated user account

        Args:
            anonymous_id: Anonymous user ID (e.g., 'anon_uuid')
            user_id: Real authenticated user ID from Cognito

        Returns:
            Dictionary with migration status for each data type
        """
        if not anonymous_id or not anonymous_id.startswith("anon_"):
            logger.warning(f"Invalid anonymous_id: {anonymous_id}, skipping linking")
            return {"profile_migrated": False, "quiz_results_migrated": False}

        logger.info(f"Starting anonymous data linking: {anonymous_id} → {user_id}")

        results = {
            "profile_migrated": False,
            "quiz_results_migrated": False,
        }

        try:
            # Migrate archetype profile
            profile_success = await self._migrate_archetype_profile(
                anonymous_id, user_id
            )
            results["profile_migrated"] = profile_success

            # Migrate quiz results
            quiz_success = await self._migrate_quiz_results(anonymous_id, user_id)
            results["quiz_results_migrated"] = quiz_success

            if profile_success:
                logger.info(f"Successfully migrated archetype profile for {user_id}")
            if quiz_success:
                logger.info(f"Successfully migrated quiz results for {user_id}")

            if profile_success or quiz_success:
                logger.info(f"✅ Linked anonymous data for {user_id}: {results}")
            else:
                logger.info(f"No anonymous data found to link for {anonymous_id}")

            return results

        except Exception as e:
            logger.error(
                f"Error linking anonymous data {anonymous_id} → {user_id}: {e}",
                exc_info=True,
            )
            return results

    async def _migrate_archetype_profile(self, anonymous_id: str, user_id: str) -> bool:
        """Migrate archetype profile from anonymous ID to real user ID"""
        try:
            # Get the anonymous profile
            anon_profile = await self.dynamodb.get_user_archetype_profile(anonymous_id)

            if not anon_profile:
                logger.debug(
                    f"No archetype profile found for anonymous user {anonymous_id}"
                )
                return False

            # Check if user already has a profile
            existing_profile = await self.dynamodb.get_user_archetype_profile(user_id)

            if existing_profile:
                logger.info(
                    f"User {user_id} already has archetype profile, skipping migration"
                )
                return False

            # Update the user_id in the profile
            anon_profile["user_id"] = user_id

            # Save under the new user_id
            await self.dynamodb.save_user_archetype_profile(anon_profile)

            # Delete the old anonymous profile to prevent duplicates
            await self.dynamodb.delete_user_archetype_profile(anonymous_id)

            logger.info(f"✅ Migrated archetype profile: {anonymous_id} → {user_id}")
            return True

        except Exception as e:
            logger.error(
                f"Error migrating archetype profile {anonymous_id} → {user_id}: {e}",
                exc_info=True,
            )
            return False

    async def _migrate_quiz_results(self, anonymous_id: str, user_id: str) -> bool:
        """Migrate all quiz results from anonymous ID to real user ID"""
        try:
            # Get all quiz results for the anonymous user
            anon_quiz_results = await self.dynamodb.get_user_quiz_results(anonymous_id)

            if not anon_quiz_results:
                logger.debug(f"No quiz results found for anonymous user {anonymous_id}")
                return False

            # Update each quiz result with the new user_id
            migrated_count = 0
            for quiz_result in anon_quiz_results:
                quiz_result["user_id"] = user_id
                await self.dynamodb.save_quiz_results(quiz_result)
                migrated_count += 1

            logger.info(
                f"✅ Migrated {migrated_count} quiz result(s): {anonymous_id} → {user_id}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Error migrating quiz results {anonymous_id} → {user_id}: {e}",
                exc_info=True,
            )
            return False
