"""
Storage quota service for Echo Vault storage management
"""

import logging
import os
from decimal import Decimal
from typing import Dict

import boto3

logger = logging.getLogger(__name__)


class StorageQuotaService:
    """
    Calculate and enforce Echo Vault storage quotas
    """

    def __init__(self, dynamodb_service):
        """
        Initialize storage quota service

        Args:
            dynamodb_service: DynamoDB service for user profile operations
        """
        self.dynamodb = dynamodb_service
        self.s3_client = boto3.client("s3")
        self.bucket_name = os.getenv("ECHO_MEDIA_BUCKET", "echo-vault-storage-dev")

    async def calculate_user_storage_usage(self, user_id: str) -> float:
        """
        Calculate total storage used by user in GB

        Args:
            user_id: User's Cognito sub

        Returns:
            float: Storage used in GB
        """
        try:
            total_bytes = 0
            prefix = f"users/{user_id}/"

            # Use paginator to handle large numbers of objects
            paginator = self.s3_client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self.bucket_name, Prefix=prefix)

            for page in pages:
                if "Contents" in page:
                    total_bytes += sum(obj["Size"] for obj in page["Contents"])

            # Convert bytes to GB
            total_gb = total_bytes / (1024**3)
            return round(total_gb, 2)

        except Exception as e:
            logger.error(f"Error calculating storage for user {user_id}: {e}")
            # Return 0 on error to avoid blocking user
            return 0.0

    async def update_user_quota(self, user_id: str) -> bool:
        """
        Update user's quota based on active subscriptions

        Args:
            user_id: User's Cognito sub

        Returns:
            bool: Success status
        """
        try:
            user_profile = await self.dynamodb.get_user_profile(user_id)
            if not user_profile:
                raise ValueError(f"User not found: {user_id}")

            # Calculate new quota based on subscription tier
            base_quota = 0.0

            if user_profile.subscription_tier in ["trial", "core", "core_plus"]:
                base_quota = 50.0  # Mirror Core includes 50GB

            if user_profile.storage_add_on_active:
                base_quota += 100.0  # Storage add-on adds 100GB

            # Update quota
            user_profile.echo_vault_quota_gb = base_quota

            # Recalculate current usage
            current_usage = await self.calculate_user_storage_usage(user_id)
            user_profile.echo_vault_used_gb = current_usage

            await self.dynamodb.update_user_profile(user_profile)

            logger.info(
                f"Updated quota for user {user_id}: {base_quota}GB (used: {current_usage}GB)"
            )
            return True

        except Exception as e:
            logger.error(f"Error updating quota for user {user_id}: {e}")
            return False

    async def check_quota_exceeded(self, user_id: str) -> Dict:
        """
        Check if user has exceeded storage quota

        Args:
            user_id: User's Cognito sub

        Returns:
            Dict with quota status
        """
        try:
            user_profile = await self.dynamodb.get_user_profile(user_id)
            if not user_profile:
                raise ValueError(f"User not found: {user_id}")

            # Refresh usage from S3
            current_usage = await self.calculate_user_storage_usage(user_id)
            user_profile.echo_vault_used_gb = current_usage
            await self.dynamodb.update_user_profile(user_profile)

            # Convert Decimal to float for arithmetic operations
            quota_gb = (
                float(user_profile.echo_vault_quota_gb)
                if user_profile.echo_vault_quota_gb
                else 0.0
            )
            percent_used = (current_usage / quota_gb * 100) if quota_gb > 0 else 0

            return {
                "exceeded": current_usage > quota_gb,
                "usage_gb": current_usage,
                "quota_gb": quota_gb,
                "percent_used": round(percent_used, 1),
                "approaching_limit": percent_used >= 80,  # Soft limit at 80%
            }

        except Exception as e:
            logger.error(f"Error checking quota for user {user_id}: {e}")
            # Return safe defaults on error
            return {
                "exceeded": False,
                "usage_gb": 0,
                "quota_gb": 0,
                "percent_used": 0,
                "approaching_limit": False,
            }

    async def can_upload(self, user_id: str, file_size_bytes: int = 0) -> Dict:
        """
        Check if user can upload a file of given size

        Args:
            user_id: User's Cognito sub
            file_size_bytes: Size of file to upload in bytes

        Returns:
            Dict with can_upload status and details
        """
        try:
            quota_status = await self.check_quota_exceeded(user_id)

            if quota_status["quota_gb"] == 0:
                return {
                    "can_upload": False,
                    "reason": "no_quota",
                    "message": "Echo Vault access requires an active subscription.",
                }

            # Calculate what usage would be after upload
            file_size_gb = file_size_bytes / (1024**3)
            # Ensure usage_gb is float for arithmetic
            current_usage_gb = (
                float(quota_status["usage_gb"])
                if isinstance(quota_status["usage_gb"], Decimal)
                else quota_status["usage_gb"]
            )
            projected_usage = current_usage_gb + file_size_gb

            if projected_usage > quota_status["quota_gb"]:
                return {
                    "can_upload": False,
                    "reason": "quota_exceeded",
                    "message": f"Upload would exceed your {quota_status['quota_gb']}GB quota. "
                    f"Currently using {quota_status['usage_gb']}GB.",
                    "quota_status": quota_status,
                }

            return {
                "can_upload": True,
                "quota_status": quota_status,
            }

        except Exception as e:
            logger.error(f"Error checking upload permission for user {user_id}: {e}")
            # Fail open to avoid blocking legitimate users
            return {"can_upload": True, "error": str(e)}
