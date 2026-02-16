"""
Trial management service for handling free trial lifecycle
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class TrialManagementService:
    """
    Manage free trial lifecycle without payment collection
    """

    TRIAL_DURATION_DAYS = 14
    TRIAL_QUOTA_GB = 50.0

    def __init__(self, dynamodb_service, push_notification_service=None):
        """
        Initialize trial management service

        Args:
            dynamodb_service: DynamoDB service for user profile operations
            push_notification_service: Optional push notification service
        """
        self.dynamodb = dynamodb_service
        self.push_service = push_notification_service

    async def start_user_trial(self, user_id: str) -> Dict:
        """
        Initialize free trial for user (no payment required)

        Args:
            user_id: User's Cognito sub

        Returns:
            Dict with trial status and expiry info
        """
        try:
            # Fetch user profile
            user_profile = await self.dynamodb.get_user_profile(user_id)
            if not user_profile:
                raise ValueError(f"User not found: {user_id}")

            # Check if user already used trial
            if user_profile.has_used_trial:
                raise ValueError("User has already used their free trial")

            # Check if user already has active subscription
            if user_profile.subscription_status in ["active", "trial"]:
                raise ValueError("User already has an active subscription or trial")

            # Calculate trial dates
            now = datetime.now(timezone.utc)
            trial_start = now
            trial_end = now + timedelta(days=self.TRIAL_DURATION_DAYS)

            # Update user profile
            user_profile.subscription_status = "trial"
            user_profile.subscription_tier = "trial"
            user_profile.trial_started_at = trial_start.isoformat().replace(
                "+00:00", "Z"
            )
            user_profile.trial_expires_at = trial_end.isoformat().replace("+00:00", "Z")
            user_profile.has_used_trial = True
            user_profile.echo_vault_quota_gb = self.TRIAL_QUOTA_GB
            user_profile.trial_notifications_sent = []

            # Save to DynamoDB
            await self.dynamodb.update_user_profile(user_profile)

            logger.info(f"Started free trial for user {user_id}, expires {trial_end}")

            return {
                "success": True,
                "trial_started_at": user_profile.trial_started_at,
                "trial_expires_at": user_profile.trial_expires_at,
                "days_remaining": self.TRIAL_DURATION_DAYS,
                "quota_gb": self.TRIAL_QUOTA_GB,
            }

        except Exception as e:
            logger.error(f"Error starting trial for user {user_id}: {e}")
            raise

    async def get_trial_status(self, user_id: str) -> Dict:
        """
        Get trial status for user

        Args:
            user_id: User's Cognito sub

        Returns:
            Dict with trial state and days remaining
        """
        try:
            user_profile = await self.dynamodb.get_user_profile(user_id)
            if not user_profile:
                raise ValueError(f"User not found: {user_id}")

            # Check if user has used trial
            if not user_profile.has_used_trial:
                return {
                    "trial_available": True,
                    "trial_status": "not_started",
                    "has_used_trial": False,
                }

            # Check if trial is active
            if (
                user_profile.subscription_status == "trial"
                and user_profile.trial_expires_at
            ):
                trial_end = datetime.fromisoformat(
                    user_profile.trial_expires_at.replace("Z", "+00:00")
                )
                now = datetime.now(timezone.utc)
                days_remaining = max(0, (trial_end - now).days)

                return {
                    "trial_available": False,
                    "trial_status": "active" if trial_end > now else "expired",
                    "trial_started_at": user_profile.trial_started_at,
                    "trial_expires_at": user_profile.trial_expires_at,
                    "days_remaining": days_remaining,
                    "has_used_trial": True,
                }

            # Trial has been used but is no longer active
            return {
                "trial_available": False,
                "trial_status": user_profile.subscription_status,
                "has_used_trial": True,
                "trial_started_at": user_profile.trial_started_at,
                "trial_expires_at": user_profile.trial_expires_at,
            }

        except Exception as e:
            logger.error(f"Error getting trial status for user {user_id}: {e}")
            raise

    async def check_trial_expiration(self) -> Dict:
        """
        Scheduled job to check for trial expirations and send notifications

        Returns:
            Dict with counts of processed users
        """
        try:
            # This would typically be called by a Lambda scheduled event
            # Scan all users with active trials
            users_to_notify_7_day = []
            users_to_notify_3_day = []
            users_to_notify_1_day = []
            users_expired = []

            # Note: In production, implement pagination for large user bases
            all_users = await self.dynamodb.scan_users_with_trials()

            now = datetime.now(timezone.utc)

            for user in all_users:
                if not user.trial_expires_at or user.subscription_status != "trial":
                    continue

                trial_end = datetime.fromisoformat(
                    user.trial_expires_at.replace("Z", "+00:00")
                )
                days_until_expiry = (trial_end - now).days

                # Check if trial has expired
                if trial_end <= now:
                    users_expired.append(user)
                    continue

                # Check notification thresholds
                if (
                    days_until_expiry <= 7
                    and "7_day" not in user.trial_notifications_sent
                ):
                    users_to_notify_7_day.append(user)
                elif (
                    days_until_expiry <= 3
                    and "3_day" not in user.trial_notifications_sent
                ):
                    users_to_notify_3_day.append(user)
                elif (
                    days_until_expiry <= 1
                    and "1_day" not in user.trial_notifications_sent
                ):
                    users_to_notify_1_day.append(user)

            # Send notifications
            for user in users_to_notify_7_day:
                await self.send_trial_expiration_notification(user, days_remaining=7)
            for user in users_to_notify_3_day:
                await self.send_trial_expiration_notification(user, days_remaining=3)
            for user in users_to_notify_1_day:
                await self.send_trial_expiration_notification(user, days_remaining=1)

            # Handle expired trials
            for user in users_expired:
                await self.handle_trial_expired(user.user_id)

            logger.info(
                f"Trial expiration check complete: "
                f"7-day={len(users_to_notify_7_day)}, "
                f"3-day={len(users_to_notify_3_day)}, "
                f"1-day={len(users_to_notify_1_day)}, "
                f"expired={len(users_expired)}"
            )

            return {
                "success": True,
                "notifications_sent": {
                    "7_day": len(users_to_notify_7_day),
                    "3_day": len(users_to_notify_3_day),
                    "1_day": len(users_to_notify_1_day),
                },
                "trials_expired": len(users_expired),
            }

        except Exception as e:
            logger.error(f"Error checking trial expirations: {e}")
            raise

    async def send_trial_expiration_notification(
        self, user_profile, days_remaining: int
    ) -> bool:
        """
        Send push notification about trial expiration

        Args:
            user_profile: UserProfile object
            days_remaining: Days until trial expires (7, 3, or 1)

        Returns:
            bool indicating success
        """
        try:
            # Map days to notification type
            notification_type = f"{days_remaining}_day"

            # Define notification messages
            messages = {
                "7_day": "7 days left in your trial. Subscribe to keep your Echo Vault.",
                "3_day": "3 days left. Don't lose access to your memories.",
                "1_day": "1 day left. Subscribe now to continue.",
            }

            message = messages.get(notification_type, "Your trial is ending soon.")

            # Send push notification if service available
            if self.push_service:
                await self.push_service.send_notification(
                    user_id=user_profile.user_id,
                    title="Mirror Collective Trial Ending",
                    body=message,
                    data={"type": "trial_expiration", "days_remaining": days_remaining},
                )

            # Update notifications sent list
            if notification_type not in user_profile.trial_notifications_sent:
                user_profile.trial_notifications_sent.append(notification_type)
                await self.dynamodb.update_user_profile(user_profile)

            logger.info(
                f"Sent {notification_type} notification to user {user_profile.user_id}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Error sending trial notification to {user_profile.user_id}: {e}"
            )
            return False

    async def handle_trial_expired(self, user_id: str) -> Dict:
        """
        Handle trial expiration - lock Echo Vault access

        Args:
            user_id: User's Cognito sub

        Returns:
            Dict with updated status
        """
        try:
            user_profile = await self.dynamodb.get_user_profile(user_id)
            if not user_profile:
                raise ValueError(f"User not found: {user_id}")

            # Check if user has active paid subscription
            if user_profile.primary_subscription_id:
                # User has paid subscription, don't downgrade
                logger.info(f"User {user_id} trial expired but has active subscription")
                return {
                    "success": True,
                    "action": "no_change",
                    "reason": "has_subscription",
                }

            # Update user profile to trial_expired
            user_profile.subscription_status = "trial_expired"
            user_profile.subscription_tier = "free"
            user_profile.echo_vault_quota_gb = 0.0  # Lock Echo Vault

            # Send final notification if not already sent
            if "expired" not in user_profile.trial_notifications_sent:
                user_profile.trial_notifications_sent.append("expired")

                if self.push_service:
                    await self.push_service.send_notification(
                        user_id=user_id,
                        title="Trial Expired",
                        body="Your trial has expired. Subscribe to unlock Echo Vault.",
                        data={"type": "trial_expired", "action": "upgrade"},
                    )

            await self.dynamodb.update_user_profile(user_profile)

            logger.info(f"Trial expired for user {user_id}, Echo Vault locked")

            return {
                "success": True,
                "action": "trial_expired",
                "subscription_status": user_profile.subscription_status,
                "quota_gb": user_profile.echo_vault_quota_gb,
            }

        except Exception as e:
            logger.error(f"Error handling trial expiration for {user_id}: {e}")
            raise
