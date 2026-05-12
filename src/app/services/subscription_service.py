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

    async def _verify_apple_notification(self, signed_payload: str) -> Optional[Dict]:
        """
        Verify an Apple App Store Server Notifications v2 payload.

        Replaces the previous insecure `jwt.decode(..., verify_signature=False)`
        path with x5c chain verification against Apple's bundled root CA
        (see src/app/services/apple_app_store_client.py).

        Returns the decoded notification dict on success, None if the
        signature is invalid or the verifier is misconfigured. Callers
        must treat None as a HARD failure — do not process unverified
        webhook payloads.
        """
        from .apple_app_store_client import (
            AppleClientConfigError,
            AppleSignatureVerificationError,
            verify_signed_notification,
        )

        try:
            return verify_signed_notification(signed_payload)
        except AppleSignatureVerificationError as exc:
            logger.warning("Rejecting Apple webhook: invalid signature: %s", exc)
            return None
        except AppleClientConfigError as exc:
            logger.error("Apple verifier misconfigured: %s", exc)
            return None

    async def _verify_apple_transaction(
        self, signed_transaction: str
    ) -> Optional[Dict]:
        """Verify an embedded signedTransactionInfo from an ASSN v2 body."""
        from .apple_app_store_client import (
            AppleClientConfigError,
            AppleSignatureVerificationError,
            verify_signed_transaction,
        )

        try:
            return verify_signed_transaction(signed_transaction)
        except AppleSignatureVerificationError as exc:
            logger.warning("Rejecting Apple transaction: invalid signature: %s", exc)
            return None
        except AppleClientConfigError as exc:
            logger.error("Apple verifier misconfigured: %s", exc)
            return None

    # Back-compat alias for callers still referring to the old name.
    # All new code should call _verify_apple_notification or
    # _verify_apple_transaction directly so the intent is explicit.
    async def _verify_apple_jwt(self, signed_payload: str) -> Optional[Dict]:
        return await self._verify_apple_notification(signed_payload)

    async def _verify_google_pubsub_message(self, message_data: str) -> Optional[Dict]:
        """
        Decode a base64 Pub/Sub message payload.

        NOTE: this does NOT verify the OIDC JWT — that's
        `_verify_google_pubsub_jwt` and must be called separately by
        the route handler with the inbound Authorization header.
        Splitting decode/verify lets us return clearer error responses
        (`401 invalid signature` vs `400 malformed body`).
        """
        try:
            decoded_data = base64.b64decode(message_data)
            notification = json.loads(decoded_data)
            logger.info("Decoded Google Pub/Sub notification")
            return notification
        except Exception as e:
            logger.error(f"Error decoding Google Pub/Sub message: {e}")
            return None

    async def _verify_google_pubsub_jwt(self, auth_header: Optional[str]) -> bool:
        """
        Verify the OIDC JWT Google attaches to each Pub/Sub push delivery.

        Google Cloud Pub/Sub push subscriptions sign every delivery with
        an OIDC token (Authorization: Bearer <jwt>). The token's claims:

            iss   = https://accounts.google.com
            aud   = the audience configured on the push subscription
                    (defaults to the endpoint URL)
            email = the service-account email configured on the push
                    subscription

        We verify the JWT signature against Google's published certs
        and require the audience + service-account email to match the
        configured env vars. Without this check, anyone who knows the
        webhook URL could forge an "Apple cancelled their subscription"
        payload and revoke a user's entitlement.

        Required env vars (see docs/IAP_STORE_SETUP.md §B3):
          GOOGLE_PUBSUB_AUDIENCE              — push subscription audience
          GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL — push subscription service account

        Returns True iff the JWT is present, signature-valid, and the
        audience + sa-email claims match. False on any failure (logged).

        Setting `GOOGLE_PUBSUB_VERIFY=false` (intentional, env-gated)
        bypasses this check — useful for local dev against a mocked
        webhook, but MUST be unset in production.
        """
        if (os.getenv("GOOGLE_PUBSUB_VERIFY", "true") or "").lower() in (
            "0",
            "false",
            "no",
        ):
            logger.warning(
                "GOOGLE_PUBSUB_VERIFY is disabled — Pub/Sub JWT not verified. "
                "Do NOT use this setting in production."
            )
            return True

        if not auth_header:
            logger.warning("Google webhook missing Authorization header")
            return False

        if not auth_header.lower().startswith("bearer "):
            logger.warning("Google webhook Authorization header missing 'Bearer '")
            return False

        token = auth_header.split(" ", 1)[1].strip()
        if not token:
            return False

        expected_audience = os.getenv("GOOGLE_PUBSUB_AUDIENCE")
        expected_email = os.getenv("GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL")
        if not expected_audience or not expected_email:
            logger.error(
                "Google Pub/Sub JWT verification misconfigured: "
                "GOOGLE_PUBSUB_AUDIENCE and "
                "GOOGLE_PUBSUB_SERVICE_ACCOUNT_EMAIL are required."
            )
            return False

        try:
            from google.auth.transport import requests as google_requests
            from google.oauth2 import id_token

            claims = id_token.verify_oauth2_token(
                token,
                google_requests.Request(),
                audience=expected_audience,
            )
        except Exception as exc:
            logger.warning("Google Pub/Sub JWT verification failed: %s", exc)
            return False

        if claims.get("email") != expected_email:
            logger.warning(
                "Google Pub/Sub JWT email mismatch: got %s, expected %s",
                claims.get("email"),
                expected_email,
            )
            return False

        if not claims.get("email_verified", False):
            logger.warning("Google Pub/Sub JWT email_verified=false")
            return False

        return True

    async def verify_and_activate_purchase(
        self,
        user_id: str,
        platform: str,
        receipt_data: str,
        product_id: str,
        transaction_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Verify an IAP receipt and (idempotently) activate the subscription.

        Idempotency key: the platform's `original_transaction_id` (iOS)
        or order base (Android). Repeated calls for the same transaction
        return the existing record without re-firing
        SUBSCRIPTION_PURCHASED events or overwriting trial/expiry data.

        Args:
            user_id: Cognito sub of the purchasing user.
            platform: "ios" or "android".
            receipt_data: Legacy field — accepted but ignored on iOS in
                favour of `transaction_id`. Still passed through on
                Android as the purchase token.
            product_id: Product SKU the client claims this purchase is for.
            transaction_id: Apple's originalTransactionId (iOS) or
                Google's orderId (Android). Authoritative identifier.

        Returns:
            Dict with subscription details. Idempotent: same
            transaction_id → same response, no double-activation.

        Raises:
            ValueError: receipt validation failed (forged / sandbox /
                unknown product / Apple JWS bad signature).
            InternalServerError: database write failed.
        """
        try:
            # Defence in depth: refuse SKUs we don't actually sell. Forged
            # receipts may claim made-up products to confuse the
            # entitlement flow.
            from ..constants.products import is_known_sku

            if not is_known_sku(product_id):
                raise ValueError(f"Unknown product_id: {product_id}")

            # 1. Validate receipt with platform.
            if platform.lower() == "ios":
                validation_result = await self.receipt_validator.validate_apple_receipt(
                    receipt_data,
                    original_transaction_id=transaction_id,
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

            transaction_data = validation_result["data"]

            # Cross-check: the SKU in the verified transaction must
            # match the product_id the client claimed. Otherwise we'd
            # accept a $0.99 receipt and grant $15.99/mo Core.
            verified_product = transaction_data.get("product_id")
            if verified_product and verified_product != product_id:
                raise ValueError(
                    f"Product mismatch: client claimed {product_id} but "
                    f"verified receipt is for {verified_product}"
                )

            # 2. Idempotency key — original_transaction_id on iOS, order
            # id on Android. Always derived from the verified receipt,
            # never from the client.
            original_txn_id = transaction_data.get(
                "original_transaction_id"
            ) or transaction_data.get("transaction_id")
            if not original_txn_id:
                raise ValueError("Verified receipt missing original_transaction_id")

            # 3. Idempotency check: if we've already activated this exact
            # transaction, return the existing record. Skips the
            # SUBSCRIPTION_PURCHASED event so analytics aren't
            # double-counted on duplicate client retries.
            existing = await self.dynamodb_service.get_item(
                self.subscriptions_table,
                {"user_id": user_id, "subscription_id": original_txn_id},
            )
            if existing:
                logger.info(
                    "Idempotent /verify-purchase for user %s txn %s — returning existing record",
                    user_id,
                    original_txn_id,
                )
                return {
                    "success": True,
                    "subscription": existing,
                    "message": "Subscription already activated",
                    "idempotent": True,
                }

            # 4. New activation — create the Subscription record.
            subscription_type, billing_period = self._parse_product_id(product_id)
            is_trial = bool(transaction_data.get("is_trial_period"))
            sub_status = (
                SubscriptionStatus.TRIAL if is_trial else SubscriptionStatus.ACTIVE
            )

            subscription = Subscription(
                user_id=user_id,
                subscription_id=original_txn_id,
                product_id=product_id,
                subscription_type=subscription_type,
                platform=(
                    Platform.IOS if platform.lower() == "ios" else Platform.ANDROID
                ),
                status=sub_status,
                billing_period=billing_period,
                price_usd=transaction_data.get("price", 0.0),
                currency_code=transaction_data.get("currency_code", "USD"),
                purchase_date=transaction_data.get("purchase_date"),
                expiry_date=transaction_data.get("expiry_date"),
                auto_renew_enabled=transaction_data.get("auto_renew_enabled", True),
                receipt_data=receipt_data,
                original_transaction_id=original_txn_id,
                is_in_trial=is_trial,
                validation_environment=(
                    "sandbox"
                    if transaction_data.get("environment") == "Sandbox"
                    else "production"
                ),
            )

            # Atomic conditional put — closes the race where two
            # concurrent /verify-purchase calls both saw existing=None
            # above and would otherwise both proceed to activate. The
            # loser gets `created=False`, re-reads the winner's row, and
            # returns it idempotently.
            created = await self.dynamodb_service.put_item_if_not_exists(
                self.subscriptions_table,
                subscription.to_dynamodb_item(),
                key_attr="subscription_id",
            )
            if not created:
                race_winner = await self.dynamodb_service.get_item(
                    self.subscriptions_table,
                    {"user_id": user_id, "subscription_id": original_txn_id},
                )
                logger.info(
                    "Concurrent /verify-purchase for user %s txn %s — returning race-winner",
                    user_id,
                    original_txn_id,
                )
                return {
                    "success": True,
                    "subscription": race_winner or subscription.to_dict(),
                    "message": "Subscription already activated",
                    "idempotent": True,
                }

            await self._update_user_subscription_status(user_id, subscription)

            await self._log_subscription_event(
                user_id=user_id,
                subscription_id=subscription.subscription_id,
                event_type="SUBSCRIPTION_PURCHASED",
                platform=platform,
                metadata={
                    "product_id": product_id,
                    "price": subscription.price_usd,
                    "expiry_date": subscription.expiry_date,
                    "is_trial": is_trial,
                },
            )

            logger.info(
                "Subscription activated user=%s txn=%s product=%s trial=%s",
                user_id,
                subscription.subscription_id,
                product_id,
                is_trial,
            )

            return {
                "success": True,
                "subscription": subscription.to_dict(),
                "message": "Subscription activated successfully",
                "idempotent": False,
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

                        # SKU whitelist + cross-check (defence in depth).
                        # Mirrors verify_and_activate_purchase — a forged
                        # receipt claiming an unknown product or a
                        # different product than the caller's claim must
                        # be rejected on the restore path too.
                        from ..constants.products import is_known_sku

                        verified_product = transaction_data.get("product_id")
                        if not verified_product or not is_known_sku(verified_product):
                            logger.warning(
                                "Rejecting restore for user %s — unknown SKU %s",
                                user_id,
                                verified_product,
                            )
                            errors.append(
                                f"Unknown product_id in receipt: {verified_product}"
                            )
                            continue

                        # Idempotency: skip if we already activated this
                        # transaction (use the verified original_transaction_id
                        # — never the client-claimed value).
                        idempotency_id = (
                            transaction_data.get("original_transaction_id")
                            or transaction_data["transaction_id"]
                        )
                        existing = await self.dynamodb_service.get_item(
                            self.subscriptions_table,
                            {
                                "user_id": user_id,
                                "subscription_id": idempotency_id,
                            },
                        )

                        if not existing:
                            # Create subscription record
                            product_id = verified_product
                            subscription_type, billing_period = self._parse_product_id(
                                product_id
                            )

                            subscription = Subscription(
                                user_id=user_id,
                                subscription_id=idempotency_id,
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
        Process an Apple App Store Server Notification v2.

        Both layers — the outer notification envelope and the inner
        signedTransactionInfo — are JWS-verified against Apple's root
        CA via the SignedDataVerifier. Anything that fails signature
        check is rejected with `{"success": False, "error": ...}` and
        the route layer surfaces that as 401, so a forged ASSN v2
        payload never reaches the lifecycle handlers.

        Args:
            notification_payload: `{"signedPayload": "<JWS>"}` body
                posted by Apple to /api/subscriptions/webhook/apple.

        Returns:
            Dict with processing status. Always returns a dict — never
            raises — so the route handler can return a predictable
            response shape regardless of whether the payload was valid.
        """
        try:
            signed_payload = notification_payload.get("signedPayload")
            if not signed_payload:
                logger.warning("Apple webhook missing signedPayload")
                return {
                    "success": False,
                    "error": "Missing signedPayload",
                    "status_code": 400,
                }

            # 1. Verify the outer notification envelope.
            decoded_payload = await self._verify_apple_notification(signed_payload)
            if not decoded_payload:
                # Signature verification already logged the reason.
                return {
                    "success": False,
                    "error": "Apple notification signature could not be verified",
                    "status_code": 401,
                }

            notification_type = decoded_payload.get("notificationType")
            data = decoded_payload.get("data", {}) or {}

            # 2. Verify the inner signedTransactionInfo if present. The
            # SDK already nested-verifies but we re-verify defensively
            # to make sure no caller bypasses the per-transaction check.
            signed_transaction_info = data.get("signedTransactionInfo")
            transaction_info = None
            if signed_transaction_info:
                transaction_info = await self._verify_apple_transaction(
                    signed_transaction_info
                )
                if transaction_info is None:
                    return {
                        "success": False,
                        "error": "Apple transaction signature could not be verified",
                        "status_code": 401,
                    }

            logger.info(
                "Processing Apple webhook notification_type=%s", notification_type
            )

            # 3. Dispatch by notification type.
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

    async def handle_google_webhook(
        self,
        notification_payload: Dict,
        auth_header: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process a Google Play Real-time Developer Notification.

        Pub/Sub push payload:
            {"message": {"data": "<base64>", "messageId": "...", ...}}

        Security: BEFORE decoding, we verify the OIDC JWT Google
        attaches to the request (Authorization: Bearer <jwt>). The
        token's audience + service-account email must match the
        env-configured push subscription. Without this, anyone who
        learns the webhook URL could forge a "subscription cancelled"
        event and revoke a user's entitlement.

        Args:
            notification_payload: Pub/Sub webhook body.
            auth_header: Raw Authorization header from the request.

        Returns:
            Dict with processing status. `status_code` is set to 401
            when the JWT fails verification so the route layer can
            propagate the right HTTP code.
        """
        try:
            # 1. Verify the Pub/Sub OIDC JWT.
            if not await self._verify_google_pubsub_jwt(auth_header):
                return {
                    "success": False,
                    "error": "Pub/Sub push token could not be verified",
                    "status_code": 401,
                }

            # 2. Extract + decode the message payload.
            message = notification_payload.get("message", {})
            message_data = message.get("data")

            if not message_data:
                logger.warning("Missing message data in Google webhook")
                return {
                    "success": False,
                    "error": "Missing message data",
                    "status_code": 400,
                }

            notification = await self._verify_google_pubsub_message(message_data)
            if not notification:
                logger.warning("Failed to decode Google Pub/Sub message")
                return {
                    "success": False,
                    "error": "Invalid Pub/Sub message",
                    "status_code": 400,
                }

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

    def _parse_product_id(
        self, product_id: str
    ) -> tuple[SubscriptionType, BillingPeriod]:
        """
        Resolve a SKU to its (SubscriptionType, BillingPeriod) pair via
        the canonical products.py catalog.

        Previous implementation used substring matching (`"core" in ...`)
        which would misclassify a crafted SKU like
        `com.attacker.core.monthly.evil`. The catalog is the single
        source of truth — if the SKU isn't there, the caller has
        already failed `is_known_sku` (defence in depth) and we'd
        never reach this method for a forged input. But to make this
        function safe even if called directly, we explicitly raise
        ValueError on unknown SKUs rather than falling back to defaults.
        """
        from ..constants.products import BillingPeriod as ProductBillingPeriod
        from ..constants.products import ProductKind, descriptor_for_sku

        descriptor = descriptor_for_sku(product_id)
        if descriptor is None:
            raise ValueError(f"Unknown product_id: {product_id}")

        if descriptor.kind == ProductKind.CORE:
            sub_type = SubscriptionType.MIRROR_CORE
        elif descriptor.kind == ProductKind.STORAGE:
            sub_type = SubscriptionType.STORAGE_ADD_ON
        else:  # pragma: no cover — exhaustive over ProductKind enum
            raise ValueError(f"Unsupported product kind: {descriptor.kind}")

        billing = (
            BillingPeriod.MONTHLY
            if descriptor.billing_period == ProductBillingPeriod.MONTHLY
            else BillingPeriod.YEARLY
        )

        return sub_type, billing

    async def _update_user_subscription_status(
        self, user_id: str, subscription: Subscription
    ) -> None:
        """
        Update user profile with subscription changes.

        Derives the profile's `subscription_status` from the
        Subscription row's status — NOT a hardcoded "active". Trial
        activations were previously writing status="active" while the
        Subscription row said TRIAL, causing the frontend and
        require_entitled dependency to disagree about whether a user
        was in a trial.

        Profile updates are applied as a single replace at the end
        (no in-place mutation between awaits) so a concurrent reader
        never sees a partially-mutated profile.
        """
        try:
            user_profile = await self.dynamodb_service.get_user_profile(user_id)
            if not user_profile:
                raise ValueError("User not found")

            # Derive profile status from the subscription row's status.
            # SubscriptionStatus.TRIAL -> "trial", ACTIVE -> "active",
            # GRACE_PERIOD -> "grace_period", etc. Matches the keys the
            # frontend useEntitlement predicate consumes.
            new_status = subscription.status.value

            # Tier resolution — apply the same precedence the previous
            # in-place code had, but compute into locals first.
            new_tier = user_profile.subscription_tier
            new_primary_sub_id = user_profile.primary_subscription_id
            new_storage_sub_id = user_profile.storage_subscription_id
            new_storage_addon_active = user_profile.storage_add_on_active

            if subscription.subscription_type == SubscriptionType.MIRROR_CORE:
                new_tier = "core"
                new_primary_sub_id = subscription.subscription_id
                base_quota = 50.0
            elif subscription.subscription_type == SubscriptionType.STORAGE_ADD_ON:
                new_storage_addon_active = True
                new_storage_sub_id = subscription.subscription_id
                base_quota = user_profile.echo_vault_quota_gb + 100.0
            else:
                base_quota = user_profile.echo_vault_quota_gb

            total_quota = base_quota
            if new_tier == "core" and new_storage_addon_active:
                new_tier = "core_plus"
                total_quota = 150.0

            new_last_check = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )

            # Apply all updates in one shot. We still mutate the
            # dataclass before passing to update_user_profile (the
            # downstream API expects a full UserProfile) but the
            # mutations happen contiguously, with no awaits in
            # between — so a concurrent reader can never observe a
            # half-updated profile.
            user_profile.subscription_status = new_status
            user_profile.subscription_tier = new_tier
            user_profile.primary_subscription_id = new_primary_sub_id
            user_profile.storage_subscription_id = new_storage_sub_id
            user_profile.storage_add_on_active = new_storage_addon_active
            user_profile.echo_vault_quota_gb = total_quota
            user_profile.last_subscription_check = new_last_check

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
            # Re-raise so handle_apple_webhook / handle_google_webhook
            # propagates a 500 to Apple/Google and they retry. Swallowing
            # would mean the platform sees 200 OK and stops retrying
            # despite our subscription state never updating — silent
            # real-money state loss.
            logger.error(f"Error handling subscription renewal: {e}", exc_info=True)
            raise

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
            # See _handle_subscription_renewal — re-raise so the
            # platform retries instead of treating the webhook as
            # consumed.
            logger.error(f"Error handling renewal failure: {e}", exc_info=True)
            raise

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
            raise

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
            raise

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
            raise
