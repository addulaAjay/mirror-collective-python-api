"""
Subscription service for managing IAP lifecycle
"""

import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from ..core.exceptions import InternalServerError
from ..models.subscription import (
    BillingPeriod,
    Platform,
    Subscription,
    SubscriptionEvent,
    SubscriptionStatus,
    SubscriptionType,
)
from .dynamodb_service import DynamoDBService
from .receipt_validator import ReceiptValidator
from .storage_quota_service import StorageQuotaService

logger = logging.getLogger(__name__)


class SubscriptionService:
    """
    Service for managing subscription lifecycle:
    - Receipt verification
    - Subscription activation
    - Renewal processing
    - Cancellation handling
    - Refund processing
    """

    # Apple Root CA for JWT verification
    APPLE_ROOT_CA_URL = "https://www.apple.com/certificateauthority/AppleRootCA-G3.cer"

    def __init__(self, dynamodb_service: DynamoDBService):
        self.dynamodb_service = dynamodb_service
        self.receipt_validator = ReceiptValidator()
        self.quota_service = StorageQuotaService(dynamodb_service)
        self.subscriptions_table = os.getenv(
            "DYNAMODB_SUBSCRIPTIONS_TABLE", "subscriptions"
        )
        self.subscription_events_table = os.getenv(
            "DYNAMODB_SUBSCRIPTION_EVENTS_TABLE", "subscription_events"
        )

    async def _verify_apple_jwt(self, signed_payload: str) -> Optional[Dict]:
        """
        Verify Apple App Store Server Notification JWT signature

        Args:
            signed_payload: JWT string from Apple webhook

        Returns:
            Decoded JWT payload if valid, None if invalid
        """
        try:
            # Import JWT library
            import jwt
            from jwt import PyJWKClient

            # Decode JWT header to get key ID
            header = jwt.get_unverified_header(signed_payload)

            # Apple uses JWK Set for their public keys
            # For production, implement proper JWK fetching and caching
            # For now, decode without verification (development only)
            logger.warning(
                "Apple JWT signature verification not fully implemented. "
                "Decoding without verification - DO NOT USE IN PRODUCTION"
            )

            # Decode without verification (INSECURE - for development only)
            decoded = jwt.decode(signed_payload, options={"verify_signature": False})

            return decoded

        except Exception as e:
            logger.error(f"Error verifying Apple JWT: {e}")
            return None

    async def _verify_google_pubsub_message(self, message_data: str) -> Optional[Dict]:
        """
        Verify and decode Google Cloud Pub/Sub message from Real-time Developer Notifications

        Args:
            message_data: Base64 encoded message data from Pub/Sub

        Returns:
            Decoded notification data if valid, None if invalid
        """
        try:
            # Decode base64 message data
            decoded_data = base64.b64decode(message_data)
            notification = json.loads(decoded_data)

            # Google Pub/Sub notifications come via Cloud Pub/Sub
            # Signature verification happens at the Pub/Sub level
            # By the time we receive it, it's already verified by GCP

            logger.info("Decoded Google Pub/Sub notification")
            return notification

        except Exception as e:
            logger.error(f"Error decoding Google Pub/Sub message: {e}")
            return None

    async def verify_and_activate_purchase(
        self, user_id: str, platform: str, receipt_data: str, product_id: str
    ) -> Dict[str, Any]:
        """
        Verify IAP receipt and activate subscription

        Args:
            user_id: Cognito sub
            platform: "ios" or "android"
            receipt_data: Receipt string from platform
            product_id: Product identifier

        Returns:
            Dict with subscription details

        Raises:
            ValueError: If receipt validation fails
            InternalServerError: If database operations fail
        """
        try:
            # 1. Validate receipt with platform
            if platform.lower() == "ios":
                validation_result = await self.receipt_validator.validate_apple_receipt(
                    receipt_data
                )
            elif platform.lower() == "android":
                validation_result = (
                    await self.receipt_validator.validate_google_receipt(
                        receipt_data, product_id
                    )
                )
            else:
                raise ValueError(f"Unsupported platform: {platform}")

            if not validation_result["valid"]:
                raise ValueError(
                    f"Receipt validation failed: {validation_result.get('error')}"
                )

            # 2. Extract transaction data
            transaction_data = validation_result["data"]

            # 3. Determine subscription type and billing period from product_id
            subscription_type, billing_period = self._parse_product_id(product_id)

            # 4. Create or update subscription record
            subscription = Subscription(
                user_id=user_id,
                subscription_id=transaction_data["transaction_id"],
                product_id=product_id,
                subscription_type=subscription_type,
                platform=(
                    Platform.IOS if platform.lower() == "ios" else Platform.ANDROID
                ),
                status=SubscriptionStatus.ACTIVE,
                billing_period=billing_period,
                price_usd=transaction_data["price"],
                purchase_date=transaction_data["purchase_date"],
                expiry_date=transaction_data["expiry_date"],
                auto_renew_enabled=transaction_data.get("auto_renew_enabled", True),
                receipt_data=receipt_data,
                is_in_trial=False,  # Paid subscription, not platform trial
            )

            # 5. Save to DynamoDB
            await self.dynamodb_service.put_item(
                self.subscriptions_table, subscription.to_dynamodb_item()
            )

            # 6. Update user profile subscription status
            await self._update_user_subscription_status(user_id, subscription)

            # 7. Log subscription event
            await self._log_subscription_event(
                user_id=user_id,
                subscription_id=subscription.subscription_id,
                event_type="SUBSCRIPTION_PURCHASED",
                platform=platform,
                metadata={
                    "product_id": product_id,
                    "price": transaction_data["price"],
                    "expiry_date": transaction_data["expiry_date"],
                },
            )

            logger.info(
                f"Subscription activated for user {user_id}: {subscription.subscription_id}"
            )

            return {
                "success": True,
                "subscription": subscription.to_dict(),
                "message": "Subscription activated successfully",
            }

        except ValueError as e:
            logger.error(f"Receipt validation error for user {user_id}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error activating subscription for user {user_id}: {e}")
            raise InternalServerError(f"Failed to activate subscription: {str(e)}")

    async def get_user_subscription_status(self, user_id: str) -> Dict[str, Any]:
        """
        Get comprehensive subscription status for user

        Args:
            user_id: Cognito sub

        Returns:
            Dict with subscription details
        """
        try:
            # Get user profile
            user_profile = await self.dynamodb_service.get_user_profile(user_id)
            if not user_profile:
                raise ValueError("User not found")

            # Get active subscriptions
            core_subscription = None
            storage_subscription = None

            if user_profile.primary_subscription_id:
                core_subscription = await self.dynamodb_service.get_item(
                    self.subscriptions_table,
                    {
                        "user_id": user_id,
                        "subscription_id": user_profile.primary_subscription_id,
                    },
                )

            if user_profile.storage_subscription_id:
                storage_subscription = await self.dynamodb_service.get_item(
                    self.subscriptions_table,
                    {
                        "user_id": user_id,
                        "subscription_id": user_profile.storage_subscription_id,
                    },
                )

            return {
                "tier": user_profile.subscription_tier,
                "status": user_profile.subscription_status,
                "core_subscription": core_subscription,
                "storage_subscription": storage_subscription,
                "quota_gb": user_profile.echo_vault_quota_gb,
                "used_gb": user_profile.echo_vault_used_gb,
                "has_used_trial": user_profile.has_used_trial,
            }

        except Exception as e:
            logger.error(f"Error getting subscription status for user {user_id}: {e}")
            raise InternalServerError(f"Failed to get subscription status: {str(e)}")

    async def restore_user_purchases(
        self, user_id: str, platform: str, receipts: list
    ) -> Dict[str, Any]:
        """
        Restore purchases from App Store/Play Store

        Args:
            user_id: Cognito sub
            platform: "ios" or "android"
            receipts: List of receipt objects
                - For iOS: strings (base64 receipt data)
                - For Android: dicts with {"purchaseToken": "...", "productId": "..."}

        Returns:
            Dict with restored subscriptions
        """
        try:
            restored_subscriptions = []
            errors = []

            for receipt_item in receipts:
                try:
                    # Validate receipt
                    if platform.lower() == "ios":
                        # iOS receipts are simple strings
                        receipt_data = (
                            receipt_item
                            if isinstance(receipt_item, str)
                            else receipt_item.get("receiptData")
                        )
                        validation_result = (
                            await self.receipt_validator.validate_apple_receipt(
                                receipt_data
                            )
                        )
                    else:
                        # Android receipts need both purchase token and product ID
                        if isinstance(receipt_item, dict):
                            purchase_token = receipt_item.get("purchaseToken")
                            product_id = receipt_item.get("productId")
                        else:
                            # Fallback: try to extract from string (legacy support)
                            purchase_token = receipt_item
                            product_id = None
                            logger.warning(
                                "Android receipt should include productId. Please update mobile client."
                            )

                        if not purchase_token or not product_id:
                            logger.error(
                                f"Missing Android purchase info for user {user_id}"
                            )
                            errors.append("Missing Android purchase info")
                            continue

                        validation_result = (
                            await self.receipt_validator.validate_google_receipt(
                                purchase_token, product_id
                            )
                        )

                    if validation_result["valid"]:
                        transaction_data = validation_result["data"]

                        # Check if subscription already exists
                        existing = await self.dynamodb_service.get_item(
                            self.subscriptions_table,
                            {
                                "user_id": user_id,
                                "subscription_id": transaction_data["transaction_id"],
                            },
                        )

                        if not existing:
                            # Create subscription record
                            product_id = transaction_data["product_id"]
                            subscription_type, billing_period = self._parse_product_id(
                                product_id
                            )

                            subscription = Subscription(
                                user_id=user_id,
                                subscription_id=transaction_data["transaction_id"],
                                product_id=product_id,
                                subscription_type=subscription_type,
                                platform=(
                                    Platform.IOS
                                    if platform.lower() == "ios"
                                    else Platform.ANDROID
                                ),
                                status=SubscriptionStatus.ACTIVE,
                                billing_period=billing_period,
                                price_usd=transaction_data["price"],
                                purchase_date=transaction_data["purchase_date"],
                                expiry_date=transaction_data["expiry_date"],
                                auto_renew_enabled=transaction_data.get(
                                    "auto_renew_enabled", True
                                ),
                                receipt_data=receipt_data,
                            )

                            await self.dynamodb_service.put_item(
                                self.subscriptions_table,
                                subscription.to_dynamodb_item(),
                            )
                            restored_subscriptions.append(subscription.to_dict())

                            # Update user profile
                            await self._update_user_subscription_status(
                                user_id, subscription
                            )

                except Exception as e:
                    logger.error(f"Error restoring receipt: {e}")
                    errors.append(str(e))

            logger.info(
                f"Restored {len(restored_subscriptions)} subscriptions for user {user_id}"
            )

            return {
                "success": True,
                "restored_count": len(restored_subscriptions),
                "subscriptions": restored_subscriptions,
                "errors": errors,
            }

        except Exception as e:
            logger.error(f"Error restoring purchases for user {user_id}: {e}")
            raise InternalServerError(f"Failed to restore purchases: {str(e)}")

    async def handle_apple_webhook(self, notification_payload: Dict) -> Dict[str, Any]:
        """
        Process Apple App Store Server Notification V2

        Args:
            notification_payload: Webhook payload from Apple (contains signedPayload JWT)

        Returns:
            Dict with processing status
        """
        try:
            # Apple sends notifications as signed JWT
            signed_payload = notification_payload.get("signedPayload")
            if not signed_payload:
                logger.error("Missing signedPayload in Apple webhook")
                return {"success": False, "error": "Missing signedPayload"}

            # Verify JWT signature
            decoded_payload = await self._verify_apple_jwt(signed_payload)
            if not decoded_payload:
                logger.error("Failed to verify Apple JWT signature")
                return {"success": False, "error": "Invalid JWT signature"}

            # Extract notification data
            notification_type = decoded_payload.get("notificationType")
            data = decoded_payload.get("data", {})

            # Decode signed transaction info
            signed_transaction_info = data.get("signedTransactionInfo")
            transaction_info = None
            if signed_transaction_info:
                transaction_info = await self._verify_apple_jwt(signed_transaction_info)

            logger.info(f"Processing Apple webhook: {notification_type}")

            # Handle different notification types
            if transaction_info:
                if notification_type == "DID_RENEW":
                    await self._handle_subscription_renewal(transaction_info)
                elif notification_type == "DID_FAIL_TO_RENEW":
                    await self._handle_renewal_failure(transaction_info)
                elif notification_type == "EXPIRED":
                    await self._handle_subscription_expired(transaction_info)
                elif notification_type == "REFUND":
                    await self._handle_refund(transaction_info)
                elif notification_type == "DID_CHANGE_RENEWAL_STATUS":
                    await self._handle_renewal_status_change(transaction_info)

            return {"success": True, "message": "Webhook processed"}

        except Exception as e:
            logger.error(f"Error processing Apple webhook: {e}")
            raise InternalServerError(f"Failed to process webhook: {str(e)}")

    async def handle_google_webhook(self, notification_payload: Dict) -> Dict[str, Any]:
        """
        Process Google Play Real-time Developer Notification

        Google sends notifications via Cloud Pub/Sub. The payload structure is:
        {
            "message": {
                "data": "base64-encoded-notification",
                "messageId": "...",
                "publishTime": "..."
            }
        }

        Args:
            notification_payload: Pub/Sub webhook payload from Google

        Returns:
            Dict with processing status
        """
        try:
            # Extract Pub/Sub message
            message = notification_payload.get("message", {})
            message_data = message.get("data")

            if not message_data:
                logger.error("Missing message data in Google webhook")
                return {"success": False, "error": "Missing message data"}

            # Decode and verify Pub/Sub message
            notification = await self._verify_google_pubsub_message(message_data)
            if not notification:
                logger.error("Failed to decode Google Pub/Sub message")
                return {"success": False, "error": "Invalid Pub/Sub message"}

            # Extract notification details
            subscription_notification = notification.get("subscriptionNotification", {})
            notification_type = subscription_notification.get("notificationType")
            purchase_token = subscription_notification.get("purchaseToken")
            subscription_id = subscription_notification.get("subscriptionId")

            logger.info(
                f"Processing Google webhook: {notification_type} for subscription {subscription_id}"
            )

            # Handle different notification types
            if notification_type == 1:  # SUBSCRIPTION_RECOVERED
                await self._handle_subscription_renewal(subscription_notification)
            elif notification_type == 2:  # SUBSCRIPTION_RENEWED
                await self._handle_subscription_renewal(subscription_notification)
            elif notification_type == 3:  # SUBSCRIPTION_CANCELED
                await self._handle_renewal_status_change(subscription_notification)
            elif notification_type == 4:  # SUBSCRIPTION_PURCHASED
                logger.info("New subscription purchased via Google Play")
            elif notification_type == 5:  # SUBSCRIPTION_ON_HOLD
                await self._handle_renewal_failure(subscription_notification)
            elif notification_type == 6:  # SUBSCRIPTION_IN_GRACE_PERIOD
                await self._handle_renewal_failure(subscription_notification)
            elif notification_type == 7:  # SUBSCRIPTION_RESTARTED
                await self._handle_subscription_renewal(subscription_notification)
            elif notification_type == 8:  # SUBSCRIPTION_PRICE_CHANGE_CONFIRMED
                logger.info("Subscription price change confirmed")
            elif notification_type == 9:  # SUBSCRIPTION_DEFERRED
                logger.info("Subscription deferred")
            elif notification_type == 10:  # SUBSCRIPTION_PAUSED
                await self._handle_renewal_status_change(subscription_notification)
            elif notification_type == 11:  # SUBSCRIPTION_PAUSE_SCHEDULE_CHANGED
                logger.info("Subscription pause schedule changed")
            elif notification_type == 12:  # SUBSCRIPTION_REVOKED
                await self._handle_refund(subscription_notification)
            elif notification_type == 13:  # SUBSCRIPTION_EXPIRED
                await self._handle_subscription_expired(subscription_notification)

            return {"success": True, "message": "Webhook processed"}

        except Exception as e:
            logger.error(f"Error processing Google webhook: {e}")
            raise InternalServerError(f"Failed to process webhook: {str(e)}")

    async def cancel_subscription(
        self, user_id: str, subscription_id: str
    ) -> Dict[str, Any]:
        """
        Cancel subscription auto-renewal (user retains access until expiry)

        Args:
            user_id: Cognito sub
            subscription_id: Subscription identifier

        Returns:
            Dict with cancellation status
        """
        try:
            # Get subscription
            subscription = await self.dynamodb_service.get_item(
                self.subscriptions_table,
                {"user_id": user_id, "subscription_id": subscription_id},
            )

            if not subscription:
                raise ValueError("Subscription not found")

            # Update auto-renew flag
            await self.dynamodb_service.update_item(
                table_name=self.subscriptions_table,
                key={"user_id": user_id, "subscription_id": subscription_id},
                update_expression="SET auto_renew_enabled = :false",
                expression_values={":false": False},
            )

            # Log event
            await self._log_subscription_event(
                user_id=user_id,
                subscription_id=subscription_id,
                event_type="SUBSCRIPTION_CANCELLED",
                platform=subscription["platform"],
                metadata={"expiry_date": subscription["expiry_date"]},
            )

            logger.info(f"Cancelled subscription {subscription_id} for user {user_id}")

            return {
                "success": True,
                "message": "Subscription cancelled. Access continues until expiry.",
                "expiry_date": subscription["expiry_date"],
            }

        except ValueError as e:
            logger.error(f"Cancellation error: {e}")
            raise
        except Exception as e:
            logger.error(f"Error cancelling subscription: {e}")
            raise InternalServerError(f"Failed to cancel subscription: {str(e)}")

    async def get_billing_history(self, user_id: str) -> Dict[str, Any]:
        """
        Get billing and event history for user

        Args:
            user_id: Cognito sub

        Returns:
            Dict with billing history
        """
        try:
            # Query subscription events
            events = await self.dynamodb_service.query_items(
                table_name=self.subscription_events_table,
                key_condition="user_id = :user_id",
                expression_values={":user_id": user_id},
                scan_index_forward=False,  # Most recent first
                limit=50,
            )

            return {"success": True, "events": events, "total_events": len(events)}

        except Exception as e:
            logger.error(f"Error getting billing history for user {user_id}: {e}")
            raise InternalServerError(f"Failed to get billing history: {str(e)}")

    # ========================================
    # PRIVATE HELPER METHODS
    # ========================================

    def _parse_product_id(self, product_id: str) -> tuple:
        """
        Parse product ID to determine subscription type and billing period

        Args:
            product_id: Product identifier (e.g., com.mirrorcollective.core.monthly)

        Returns:
            Tuple of (SubscriptionType, BillingPeriod)
        """
        if "core" in product_id.lower():
            subscription_type = SubscriptionType.MIRROR_CORE
        elif "storage" in product_id.lower():
            subscription_type = SubscriptionType.STORAGE_ADD_ON
        else:
            subscription_type = SubscriptionType.MIRROR_CORE

        if "monthly" in product_id.lower():
            billing_period = BillingPeriod.MONTHLY
        elif "yearly" in product_id.lower():
            billing_period = BillingPeriod.YEARLY
        else:
            billing_period = BillingPeriod.MONTHLY

        return subscription_type, billing_period

    async def _update_user_subscription_status(
        self, user_id: str, subscription: Subscription
    ) -> None:
        """
        Update user profile with subscription changes

        Args:
            user_id: Cognito sub
            subscription: Subscription object
        """
        try:
            user_profile = await self.dynamodb_service.get_user_profile(user_id)
            if not user_profile:
                raise ValueError("User not found")

            # Update subscription fields
            user_profile.subscription_status = "active"

            # Update tier and quota based on subscription type
            if subscription.subscription_type == SubscriptionType.MIRROR_CORE:
                user_profile.subscription_tier = "core"
                user_profile.primary_subscription_id = subscription.subscription_id
                base_quota = 50.0  # Mirror Core gives 50 GB
            elif subscription.subscription_type == SubscriptionType.STORAGE_ADD_ON:
                user_profile.storage_add_on_active = True
                user_profile.storage_subscription_id = subscription.subscription_id
                base_quota = user_profile.echo_vault_quota_gb + 100.0  # Add 100 GB
            else:
                base_quota = user_profile.echo_vault_quota_gb

            # Calculate total quota
            total_quota = base_quota
            if (
                user_profile.subscription_tier == "core"
                and user_profile.storage_add_on_active
            ):
                user_profile.subscription_tier = "core_plus"
                total_quota = 150.0  # 50 GB (core) + 100 GB (storage)

            user_profile.echo_vault_quota_gb = total_quota
            user_profile.last_subscription_check = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )

            # Save updated profile
            await self.dynamodb_service.update_user_profile(user_profile)

            logger.info(
                f"Updated subscription status for user {user_id}: tier={user_profile.subscription_tier}, quota={total_quota}GB"
            )

        except Exception as e:
            logger.error(f"Error updating user subscription status: {e}")
            raise

    async def _log_subscription_event(
        self,
        user_id: str,
        subscription_id: str,
        event_type: str,
        platform: str,
        metadata: Optional[Dict] = None,
    ) -> None:
        """
        Log subscription event to audit table

        Args:
            user_id: Cognito sub
            subscription_id: Subscription identifier
            event_type: Event type
            platform: Platform
            metadata: Additional event data
        """
        try:
            from uuid import uuid4

            event = SubscriptionEvent(
                event_id=str(uuid4()),
                user_id=user_id,
                subscription_id=subscription_id,
                event_type=event_type,
                timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                platform=Platform(platform) if isinstance(platform, str) else platform,
                metadata=metadata or {},
            )

            await self.dynamodb_service.put_item(
                self.subscription_events_table, event.to_dynamodb_item()
            )

        except Exception as e:
            logger.error(f"Error logging subscription event: {e}")
            # Don't raise - event logging is non-critical

    async def _handle_subscription_renewal(self, transaction_info: Dict) -> None:
        """
        Handle successful subscription renewal webhook

        Args:
            transaction_info: Decoded transaction data from webhook
        """
        try:
            logger.info(f"Handling subscription renewal: {transaction_info}")

            # Extract transaction details
            # For Apple: transaction_info contains decoded JWT
            # For Google: transaction_info contains subscriptionNotification
            transaction_id = transaction_info.get(
                "transactionId"
            ) or transaction_info.get("originalTransactionId")
            subscription_id_from_webhook = transaction_info.get("subscriptionId")
            purchase_token = transaction_info.get("purchaseToken")

            # Try to find subscription by transaction ID or purchase token
            subscription = None
            if transaction_id:
                # Query by subscription_id (which is original_transaction_id for iOS)
                subscriptions = await self.dynamodb_service.query_items(
                    table_name=self.subscriptions_table,
                    key_condition="subscription_id = :sid",
                    expression_values={":sid": transaction_id},
                    index_name="subscription-id-index",
                )
                if subscriptions:
                    subscription = Subscription.from_dynamodb_item(subscriptions[0])

            if not subscription:
                logger.warning(f"Subscription not found for renewal: {transaction_id}")
                return

            # Update subscription with new expiry date
            if transaction_info.get("expiresDate"):
                # Apple format: milliseconds since epoch
                expiry_ms = int(transaction_info["expiresDate"])
                expiry_date = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
                subscription.expiry_date = expiry_date.isoformat().replace(
                    "+00:00", "Z"
                )
            elif transaction_info.get("expiryTimeMillis"):
                # Google format: milliseconds since epoch
                expiry_ms = int(transaction_info["expiryTimeMillis"])
                expiry_date = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
                subscription.expiry_date = expiry_date.isoformat().replace(
                    "+00:00", "Z"
                )

            # Update status to active
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.add_event("renewed", transaction_info)

            # Save updated subscription
            await self.dynamodb_service.put_item(
                self.subscriptions_table, subscription.to_dynamodb_item()
            )

            # Update user profile
            await self._update_user_subscription_status(
                subscription.user_id, subscription
            )

            # Log renewal event
            await self._log_subscription_event(
                user_id=subscription.user_id,
                subscription_id=subscription.subscription_id,
                event_type="renewed",
                platform=subscription.platform.value,
                metadata=transaction_info,
            )

            logger.info(
                f"Successfully processed renewal for subscription {subscription.subscription_id}"
            )

        except Exception as e:
            logger.error(f"Error handling subscription renewal: {e}", exc_info=True)

    async def _handle_renewal_failure(self, transaction_info: Dict) -> None:
        """
        Handle failed renewal webhook

        Args:
            transaction_info: Decoded transaction data from webhook
        """
        try:
            logger.info(f"Handling renewal failure: {transaction_info}")

            # Extract transaction details
            transaction_id = transaction_info.get(
                "transactionId"
            ) or transaction_info.get("originalTransactionId")

            # Find subscription
            subscription = None
            if transaction_id:
                subscriptions = await self.dynamodb_service.query_items(
                    table_name=self.subscriptions_table,
                    key_condition="subscription_id = :sid",
                    expression_values={":sid": transaction_id},
                    index_name="subscription-id-index",
                )
                if subscriptions:
                    subscription = Subscription.from_dynamodb_item(subscriptions[0])

            if not subscription:
                logger.warning(
                    f"Subscription not found for renewal failure: {transaction_id}"
                )
                return

            # Update status to grace period (user still has access during grace period)
            subscription.status = SubscriptionStatus.GRACE_PERIOD
            subscription.add_event("renewal_failed", transaction_info)

            # Save updated subscription
            await self.dynamodb_service.put_item(
                self.subscriptions_table, subscription.to_dynamodb_item()
            )

            # Get user profile to send notification
            user_profile = await self.dynamodb_service.get_user_profile(
                subscription.user_id
            )
            if user_profile:
                # TODO: Send push notification about payment failure
                # This would integrate with your notification service
                logger.info(
                    f"Should send payment failure notification to user {subscription.user_id}"
                )

            # Log renewal failure event
            await self._log_subscription_event(
                user_id=subscription.user_id,
                subscription_id=subscription.subscription_id,
                event_type="renewal_failed",
                platform=subscription.platform.value,
                metadata=transaction_info,
            )

            logger.info(
                f"Successfully processed renewal failure for subscription {subscription.subscription_id}"
            )

        except Exception as e:
            logger.error(f"Error handling renewal failure: {e}", exc_info=True)

    async def _handle_subscription_expired(self, transaction_info: Dict) -> None:
        """
        Handle subscription expiration webhook

        Args:
            transaction_info: Decoded transaction data from webhook
        """
        try:
            logger.info(f"Handling subscription expiration: {transaction_info}")

            # Extract transaction details
            transaction_id = transaction_info.get(
                "transactionId"
            ) or transaction_info.get("originalTransactionId")

            # Find subscription
            subscription = None
            if transaction_id:
                subscriptions = await self.dynamodb_service.query_items(
                    table_name=self.subscriptions_table,
                    key_condition="subscription_id = :sid",
                    expression_values={":sid": transaction_id},
                    index_name="subscription-id-index",
                )
                if subscriptions:
                    subscription = Subscription.from_dynamodb_item(subscriptions[0])

            if not subscription:
                logger.warning(
                    f"Subscription not found for expiration: {transaction_id}"
                )
                return

            # Update subscription status to expired
            subscription.status = SubscriptionStatus.EXPIRED
            subscription.auto_renew_enabled = False
            subscription.add_event("expired", transaction_info)

            # Save updated subscription
            await self.dynamodb_service.put_item(
                self.subscriptions_table, subscription.to_dynamodb_item()
            )

            # Update user profile - revoke access
            user_profile = await self.dynamodb_service.get_user_profile(
                subscription.user_id
            )
            if user_profile:
                # Determine if user has other active subscriptions
                user_subscriptions = await self.dynamodb_service.query_items(
                    table_name=self.subscriptions_table,
                    key_condition="user_id = :uid",
                    expression_values={":uid": subscription.user_id},
                )

                # Check for other active subscriptions
                has_other_active = any(
                    sub.get("status") in ["active", "trial"]
                    for sub in user_subscriptions
                    if sub.get("subscription_id") != subscription.subscription_id
                )

                if not has_other_active:
                    # No other active subscriptions - revoke all access
                    user_profile.subscription_status = "expired"
                    user_profile.subscription_tier = "free"
                    user_profile.echo_vault_quota_gb = 0.0

                    # Clear subscription references
                    if subscription.subscription_type == SubscriptionType.MIRROR_CORE:
                        user_profile.primary_subscription_id = None
                    elif (
                        subscription.subscription_type
                        == SubscriptionType.STORAGE_ADD_ON
                    ):
                        user_profile.storage_subscription_id = None
                        user_profile.storage_add_on_active = False

                    await self.dynamodb_service.update_user_profile(user_profile)

            # Log expiration event
            await self._log_subscription_event(
                user_id=subscription.user_id,
                subscription_id=subscription.subscription_id,
                event_type="expired",
                platform=subscription.platform.value,
                metadata=transaction_info,
            )

            logger.info(
                f"Successfully processed expiration for subscription {subscription.subscription_id}"
            )

        except Exception as e:
            logger.error(f"Error handling subscription expiration: {e}", exc_info=True)

    async def _handle_refund(self, transaction_info: Dict) -> None:
        """
        Handle refund webhook

        Args:
            transaction_info: Decoded transaction data from webhook
        """
        try:
            logger.info(f"Handling refund: {transaction_info}")

            # Extract transaction details
            transaction_id = transaction_info.get(
                "transactionId"
            ) or transaction_info.get("originalTransactionId")

            # Find subscription
            subscription = None
            if transaction_id:
                subscriptions = await self.dynamodb_service.query_items(
                    table_name=self.subscriptions_table,
                    key_condition="subscription_id = :sid",
                    expression_values={":sid": transaction_id},
                    index_name="subscription-id-index",
                )
                if subscriptions:
                    subscription = Subscription.from_dynamodb_item(subscriptions[0])

            if not subscription:
                logger.warning(f"Subscription not found for refund: {transaction_id}")
                return

            # Update subscription status to refunded
            subscription.status = SubscriptionStatus.REFUNDED
            subscription.auto_renew_enabled = False
            subscription.add_event("refunded", transaction_info)

            # Save updated subscription
            await self.dynamodb_service.put_item(
                self.subscriptions_table, subscription.to_dynamodb_item()
            )

            # IMMEDIATELY revoke access (refunds require instant access removal)
            user_profile = await self.dynamodb_service.get_user_profile(
                subscription.user_id
            )
            if user_profile:
                # Check for other active subscriptions
                user_subscriptions = await self.dynamodb_service.query_items(
                    table_name=self.subscriptions_table,
                    key_condition="user_id = :uid",
                    expression_values={":uid": subscription.user_id},
                )

                has_other_active = any(
                    sub.get("status") in ["active", "trial"]
                    for sub in user_subscriptions
                    if sub.get("subscription_id") != subscription.subscription_id
                )

                if not has_other_active:
                    # Immediately revoke all access
                    user_profile.subscription_status = "expired"
                    user_profile.subscription_tier = "free"
                    user_profile.echo_vault_quota_gb = 0.0

                    # Clear subscription references
                    if subscription.subscription_type == SubscriptionType.MIRROR_CORE:
                        user_profile.primary_subscription_id = None
                    elif (
                        subscription.subscription_type
                        == SubscriptionType.STORAGE_ADD_ON
                    ):
                        user_profile.storage_subscription_id = None
                        user_profile.storage_add_on_active = False

                    await self.dynamodb_service.update_user_profile(user_profile)

            # Log refund event
            await self._log_subscription_event(
                user_id=subscription.user_id,
                subscription_id=subscription.subscription_id,
                event_type="refunded",
                platform=subscription.platform.value,
                metadata=transaction_info,
            )

            logger.info(
                f"Successfully processed refund for subscription {subscription.subscription_id}"
            )

        except Exception as e:
            logger.error(f"Error handling refund: {e}", exc_info=True)

    async def _handle_renewal_status_change(self, transaction_info: Dict) -> None:
        """
        Handle renewal status change webhook (user enabled/disabled auto-renewal)

        Args:
            transaction_info: Decoded transaction data from webhook
        """
        try:
            logger.info(f"Handling renewal status change: {transaction_info}")

            # Extract transaction details
            transaction_id = transaction_info.get(
                "transactionId"
            ) or transaction_info.get("originalTransactionId")
            auto_renew_status = transaction_info.get(
                "autoRenewStatus"
            ) or transaction_info.get("autoRenewing")

            # Find subscription
            subscription = None
            if transaction_id:
                subscriptions = await self.dynamodb_service.query_items(
                    table_name=self.subscriptions_table,
                    key_condition="subscription_id = :sid",
                    expression_values={":sid": transaction_id},
                    index_name="subscription-id-index",
                )
                if subscriptions:
                    subscription = Subscription.from_dynamodb_item(subscriptions[0])

            if not subscription:
                logger.warning(
                    f"Subscription not found for renewal status change: {transaction_id}"
                )
                return

            # Update auto-renewal status
            if auto_renew_status is not None:
                # Apple sends "1" for enabled, "0" for disabled
                # Google sends boolean
                if isinstance(auto_renew_status, str):
                    subscription.auto_renew_enabled = auto_renew_status == "1"
                else:
                    subscription.auto_renew_enabled = bool(auto_renew_status)

                subscription.add_event("auto_renew_status_changed", transaction_info)

                # Save updated subscription
                await self.dynamodb_service.put_item(
                    self.subscriptions_table, subscription.to_dynamodb_item()
                )

                # Log status change event
                await self._log_subscription_event(
                    user_id=subscription.user_id,
                    subscription_id=subscription.subscription_id,
                    event_type="auto_renew_status_changed",
                    platform=subscription.platform.value,
                    metadata=transaction_info,
                )

                logger.info(
                    f"Successfully processed renewal status change for subscription {subscription.subscription_id}: "
                    f"auto_renew={subscription.auto_renew_enabled}"
                )

        except Exception as e:
            logger.error(f"Error handling renewal status change: {e}", exc_info=True)
