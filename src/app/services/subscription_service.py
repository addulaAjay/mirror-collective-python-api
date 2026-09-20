"""
Subscription service for managing IAP lifecycle
"""

import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

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
from .receipt_validator import (
    JWSVerificationError,
    ReceiptValidator,
    verify_and_decode_apple_notification,
    verify_apple_renewal_info_jws,
    verify_apple_transaction_jws,
)
from .storage_quota_service import get_storage_quota_service

logger = logging.getLogger(__name__)


def _ms_to_iso(ms: Optional[Any]) -> Optional[str]:
    """Convert Apple's epoch-millis timestamp to an ISO 8601 string (or None).

    The modern App Store Server API returns dates as epoch milliseconds
    (``purchaseDate``, ``expiresDate``); the Subscription model stores ISO 8601.
    """
    if not ms:
        return None
    return (
        datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 string to an aware datetime, or None on any failure."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _later_iso(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """Return whichever ISO 8601 timestamp is later.

    Used so a subscription's stored ``expiry_date`` is only ever advanced, never
    moved backward — a stale single-transaction lookup (the original period)
    must not clobber a correct, later renewal expiry. None/unparseable operands
    defer to the other value.
    """
    da, db = _parse_iso(a), _parse_iso(b)
    if da is None:
        return b
    if db is None:
        return a
    return a if da >= db else b


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
        self.quota_service = get_storage_quota_service()
        self.subscriptions_table = os.getenv(
            "DYNAMODB_SUBSCRIPTIONS_TABLE", "subscriptions"
        )
        self.subscription_events_table = os.getenv(
            "DYNAMODB_SUBSCRIPTION_EVENTS_TABLE", "subscription_events"
        )

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
        self,
        user_id: str,
        platform: str,
        receipt_data: str,
        product_id: str,
        transaction_id: Optional[str] = None,
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
                    receipt_data, transaction_id=transaction_id
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

            # 3b. Normalise modern App Store Server API fields: dates come back as
            # epoch-millis (*_ms) and price in milliunits of the currency; the
            # Subscription model wants ISO 8601 strings and a float price.
            purchase_date_iso = _ms_to_iso(transaction_data.get("purchase_date_ms"))
            expiry_date_iso = _ms_to_iso(transaction_data.get("expires_date_ms"))
            raw_price = transaction_data.get("price")
            price_usd = round(float(raw_price) / 1000.0, 2) if raw_price else 0.0
            # A StoreKit introductory (free-trial) or intro-offer transaction is
            # a real trial — reflect it so the user is shown "trial" (not paid
            # "active") and can still convert. The parser derives these from the
            # transaction's offerType (see receipt_validator.parse_apple_transaction).
            is_trial = bool(
                transaction_data.get("is_trial_period")
                or transaction_data.get("is_in_intro_offer_period")
            )

            original_txn_id = (
                transaction_data.get("original_transaction_id")
                or transaction_data["transaction_id"]
            )

            # 3c. Resolve the *effective* expiry before writing the record.
            # A ``/transactions/{id}`` lookup only reports the period of the id
            # we were given, and clients resend the ORIGINAL transaction id — so
            # ``expiry_date_iso`` here can be the first period, weeks in the past,
            # for an actively-renewing subscription. Two guards fix that:
            #   (#2) ask Apple for the latest expiry across all renewals, and
            #   (#1) never let the stored expiry regress below what we already
            #        recorded (a stale lookup must not move it backward).
            expiry_date_iso = await self._effective_expiry_iso(
                platform=platform,
                user_id=user_id,
                subscription_id=original_txn_id,
                single_txn_expiry_iso=expiry_date_iso,
            )

            # 4. Create or update subscription record
            subscription = Subscription(
                user_id=user_id,
                subscription_id=original_txn_id,
                product_id=product_id,
                subscription_type=subscription_type,
                platform=(
                    Platform.IOS if platform.lower() == "ios" else Platform.ANDROID
                ),
                status=SubscriptionStatus.ACTIVE,
                billing_period=billing_period,
                price_usd=price_usd,
                purchase_date=purchase_date_iso,
                expiry_date=expiry_date_iso,
                auto_renew_enabled=transaction_data.get("auto_renew_status", True),
                receipt_data=receipt_data,
                is_in_trial=is_trial,
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
                    "price": price_usd,
                    "expiry_date": expiry_date_iso,
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
                                "subscription_id": transaction_data.get(
                                    "original_transaction_id"
                                )
                                or transaction_data["transaction_id"],
                            },
                        )

                        # Normalise modern App Store Server API fields exactly
                        # like the purchase path: the parser emits epoch-millis
                        # (*_ms) dates and a milliunits price — NOT
                        # purchase_date/expiry_date/price. Reading the latter
                        # keys raised KeyError, which was swallowed, so restore
                        # always returned 0 and a paying user reinstalling could
                        # never regain access.
                        product_id = transaction_data["product_id"]
                        subscription_type, billing_period = self._parse_product_id(
                            product_id
                        )
                        raw_price = transaction_data.get("price")
                        price_usd = (
                            round(float(raw_price) / 1000.0, 2) if raw_price else 0.0
                        )
                        is_trial = bool(
                            transaction_data.get("is_trial_period")
                            or transaction_data.get("is_in_intro_offer_period")
                        )

                        subscription = Subscription(
                            user_id=user_id,
                            subscription_id=transaction_data.get(
                                "original_transaction_id"
                            )
                            or transaction_data["transaction_id"],
                            product_id=product_id,
                            subscription_type=subscription_type,
                            platform=(
                                Platform.IOS
                                if platform.lower() == "ios"
                                else Platform.ANDROID
                            ),
                            status=SubscriptionStatus.ACTIVE,
                            billing_period=billing_period,
                            price_usd=price_usd,
                            purchase_date=_ms_to_iso(
                                transaction_data.get("purchase_date_ms")
                            ),
                            expiry_date=_ms_to_iso(
                                transaction_data.get("expires_date_ms")
                            ),
                            auto_renew_enabled=transaction_data.get(
                                "auto_renew_status", True
                            ),
                            receipt_data=receipt_data,
                            is_in_trial=is_trial,
                        )

                        # Upsert regardless of `existing`: a returning user often
                        # already has a record (e.g. previously marked expired),
                        # and restore must refresh its status/expiry from the
                        # fresh validation rather than skip it.
                        logger.info(
                            "Restore %s subscription %s for user %s",
                            "updating" if existing else "creating",
                            subscription.subscription_id,
                            user_id,
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
            # Apple sends notifications as a signed JWS (App Store Server
            # Notifications V2).
            signed_payload = notification_payload.get("signedPayload")
            if not signed_payload:
                logger.error("Missing signedPayload in Apple webhook")
                return {"success": False, "error": "Missing signedPayload"}

            # Verify the notification signature (x5c chain → Apple Root CA G3,
            # bundle_id, app_apple_id). Fail closed: a bad signature never
            # mutates entitlements.
            try:
                decoded_payload, sandbox = verify_and_decode_apple_notification(
                    signed_payload
                )
            except JWSVerificationError as e:
                logger.error(f"Apple webhook signature verification failed: {e}")
                return {"success": False, "error": "Invalid signature"}

            # Extract notification data
            notification_type = decoded_payload.get("notificationType")
            data = decoded_payload.get("data", {})

            # Verify the inner signed transaction info the same way.
            signed_transaction_info = data.get("signedTransactionInfo")
            transaction_info = None
            if signed_transaction_info:
                try:
                    transaction_info = verify_apple_transaction_jws(
                        signed_transaction_info, sandbox=sandbox
                    )
                except JWSVerificationError as e:
                    logger.error(f"Apple webhook transaction verification failed: {e}")
                    return {
                        "success": False,
                        "error": "Invalid transaction signature",
                    }

            # autoRenewStatus / autoRenewProductId / expirationIntent live in
            # signedRenewalInfo, NOT signedTransactionInfo. Decode it too and
            # merge those fields into transaction_info so DID_CHANGE_RENEWAL_STATUS
            # (and the renewal/expiry handlers) can actually read them — without
            # this a cancellation is received but never applied, and the record's
            # auto_renew_enabled never flips.
            signed_renewal_info = data.get("signedRenewalInfo")
            if signed_renewal_info and transaction_info is not None:
                try:
                    renewal_info = verify_apple_renewal_info_jws(
                        signed_renewal_info, sandbox=sandbox
                    )
                except JWSVerificationError as e:
                    # Non-fatal: the OUTER notification is already signature-
                    # verified, so a renewal-info decode failure must not drop the
                    # whole event. Proceed without the renewal fields rather than
                    # regressing to "nothing applied".
                    logger.warning(
                        "Apple webhook renewal-info decode failed; continuing "
                        f"without renewal fields: {e}"
                    )
                    renewal_info = {}
                for _k in (
                    "autoRenewStatus",
                    "autoRenewProductId",
                    "expirationIntent",
                    "gracePeriodExpiresDate",
                ):
                    if renewal_info.get(_k) is not None:
                        transaction_info.setdefault(_k, renewal_info.get(_k))

            logger.info(f"Processing Apple webhook: {notification_type}")

            # Handle different notification types
            if transaction_info:
                # Idempotency + ordering guard. Apple retries notifications and
                # does NOT guarantee order, so a replayed REFUND or a stale
                # EXPIRED arriving after a newer DID_RENEW could otherwise
                # clobber current state. Skip anything we've already applied or
                # that is older than the last event applied to this subscription.
                notification_uuid = decoded_payload.get("notificationUUID")
                signed_date = decoded_payload.get("signedDate")
                # Fall back to the transaction's signedDate if the notification
                # body omits it, so the idempotency guard + ordered writes always
                # have an ordering timestamp.
                if signed_date is None:
                    signed_date = transaction_info.get("signedDate")
                original_txid = transaction_info.get(
                    "originalTransactionId"
                ) or transaction_info.get("transactionId")

                if original_txid and await self._apple_notification_already_applied(
                    original_txid, notification_uuid, signed_date
                ):
                    logger.info(
                        "Ignoring stale/duplicate Apple notification "
                        f"{notification_uuid} ({notification_type})"
                    )
                    return {
                        "success": True,
                        "message": "Ignored (stale or duplicate)",
                    }

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
                elif notification_type == "DID_CHANGE_RENEWAL_PREF":
                    # Plan change (monthly<->yearly). Intentionally NOT persisted:
                    # the entitlement gate keys on tier (core), not the specific
                    # product, and the app sources the active plan from
                    # StoreKit/Apple (getAvailablePurchases + Apple's Manage
                    # Subscriptions sheet). So product_id/billing_period are not
                    # webhook-maintained — no-op by design.
                    pass

                # Record this event as applied so a later replay / out-of-order
                # delivery is ignored by the guard above.
                if original_txid:
                    await self._record_apple_notification_applied(
                        original_txid, notification_uuid, signed_date
                    )

            return {"success": True, "message": "Webhook processed"}

        except Exception as e:
            logger.error(f"Error processing Apple webhook: {e}")
            raise InternalServerError(f"Failed to process webhook: {str(e)}")

    async def _load_subscription_by_original_txid(
        self, original_txid: str
    ) -> Optional["Subscription"]:
        """Load a subscription by its stable originalTransactionId (the key
        records are stored under), or None if not found."""
        subs = await self.dynamodb_service.query_items(
            table_name=self.subscriptions_table,
            key_condition="subscription_id = :sid",
            expression_values={":sid": original_txid},
            index_name="subscription-id-index",
        )
        return Subscription.from_dynamodb_item(subs[0]) if subs else None

    async def _apple_notification_already_applied(
        self,
        original_txid: str,
        notification_uuid: Optional[str],
        signed_date: Optional[Any],
    ) -> bool:
        """True when this Apple notification is a replay or is older than the
        last event already applied to the subscription (out-of-order). A missing
        subscription (first event) or any read error → False (process it)."""
        try:
            sub = await self._load_subscription_by_original_txid(original_txid)
        except Exception as e:  # noqa: BLE001 — never drop a real event on a read blip
            logger.warning(f"Idempotency lookup failed for {original_txid}: {e}")
            return False
        if not sub:
            return False
        # Exact replay of a notification we already processed.
        if notification_uuid and (
            getattr(sub, "last_notification_uuid", None) == notification_uuid
        ):
            return True
        # Out-of-order / stale: not newer than what we've already applied.
        last = getattr(sub, "last_notification_signed_date_ms", None)
        if last and signed_date and int(signed_date) <= int(last):
            return True
        return False

    async def _record_apple_notification_applied(
        self,
        original_txid: str,
        notification_uuid: Optional[str],
        signed_date: Optional[Any],
    ) -> None:
        """Persist the last applied notification UUID + signedDate on the
        subscription so the guard can dedupe/reorder subsequent deliveries.

        Uses a TARGETED update_item (SET only the two tracking fields) rather
        than load-then-put. The subscription is loaded via the eventually-
        consistent ``subscription-id-index`` GSI, so a full re-write here would
        clobber the handler's just-applied change (e.g. a cancel flipping
        auto_renew_enabled) with a stale pre-write copy. update_item touches
        only these fields and leaves the handler's write intact."""
        if not notification_uuid and not signed_date:
            return
        try:
            sub = await self._load_subscription_by_original_txid(original_txid)
            if not sub:
                return
            set_parts = []
            values: Dict[str, Any] = {}
            if notification_uuid:
                set_parts.append("last_notification_uuid = :u")
                values[":u"] = notification_uuid
            if signed_date:
                set_parts.append("last_notification_signed_date_ms = :d")
                values[":d"] = int(signed_date)
            if not set_parts:
                return
            await self.dynamodb_service.update_item(
                self.subscriptions_table,
                {"user_id": sub.user_id, "subscription_id": sub.subscription_id},
                "SET " + ", ".join(set_parts),
                values,
            )
        except Exception as e:  # noqa: BLE001 — bookkeeping must not fail the webhook
            logger.warning(
                f"Failed to record notification state for {original_txid}: {e}"
            )

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
        # Storage add-on IDs contain "storage" (e.g. ...mirror.storage.monthly);
        # every other product is the Core plan (...mirror.monthly / .yearly).
        if "storage" in product_id.lower():
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

    async def _effective_expiry_iso(
        self,
        *,
        platform: str,
        user_id: str,
        subscription_id: str,
        single_txn_expiry_iso: Optional[str],
    ) -> Optional[str]:
        """Resolve the expiry to persist: the latest of the single-transaction
        expiry, Apple's current subscription-status expiry (#2), and whatever we
        already stored (#1 — never regress).

        Every step is best-effort and falls back to the single-transaction value
        on error, so a lookup or read failure can never fail the purchase or move
        a valid expiry backward.
        """
        effective = single_txn_expiry_iso

        # (#2) iOS only: fetch the newest verified expiry across all renewals and
        # take whichever is later than the single-transaction value.
        if platform.lower() == "ios" and subscription_id:
            try:
                latest_ms = await self.receipt_validator.get_apple_latest_expiry_ms(
                    subscription_id
                )
                effective = _later_iso(effective, _ms_to_iso(latest_ms))
            except Exception as e:  # noqa: BLE001 - best-effort; keep fallback
                logger.warning(
                    f"latest-expiry lookup failed for {subscription_id}: {e}"
                )

        # (#1) Never regress below the stored expiry — a stale write must not move
        # a previously-recorded, later expiry backward.
        try:
            existing = await self.dynamodb_service.get_item(
                self.subscriptions_table,
                {"user_id": user_id, "subscription_id": subscription_id},
            )
            if existing:
                effective = _later_iso(effective, existing.get("expiry_date"))
        except Exception as e:  # noqa: BLE001 - best-effort; keep fallback
            logger.warning(
                f"expiry non-regression read failed for {subscription_id}: {e}"
            )

        return effective

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

            # Update subscription fields. A trial/intro-offer subscription is
            # surfaced as "trial" (client shows the free-trial state and still
            # lets the user convert) rather than paid "active". Once the user
            # has consumed a trial, record it.
            sub_type = subscription.subscription_type
            if sub_type == SubscriptionType.MIRROR_CORE:
                # subscription_status tracks the CORE plan (trial vs paid).
                if getattr(subscription, "is_in_trial", False):
                    user_profile.subscription_status = "trial"
                    user_profile.has_used_trial = True
                else:
                    user_profile.subscription_status = "active"
                user_profile.primary_subscription_id = subscription.subscription_id
            elif sub_type == SubscriptionType.STORAGE_ADD_ON:
                # The add-on is an OVERLAY — it never changes the Core status.
                user_profile.storage_add_on_active = True
                user_profile.storage_subscription_id = subscription.subscription_id

            # Derive tier + quota from the flags (idempotent — never increments;
            # see UserProfile.recompute_entitlement). Fixes the previous
            # +100-per-renewal double-count.
            user_profile.recompute_entitlement()
            user_profile.last_subscription_check = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )

            # Save updated profile
            await self.dynamodb_service.update_user_profile(user_profile)

            logger.info(
                f"Updated subscription status for user {user_id}: "
                f"tier={user_profile.subscription_tier}, "
                f"quota={user_profile.echo_vault_quota_gb}GB"
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

    @staticmethod
    def _event_signed_ms(transaction_info: Dict) -> Optional[int]:
        """The event's signedDate as epoch-ms (used for advance-only ordering),
        or None if absent/unparseable."""
        raw = transaction_info.get("signedDate")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    async def _ordered_subscription_set(
        self,
        subscription: "Subscription",
        signed_ms: Optional[int],
        set_fields: Dict[str, Any],
        event_type: str,
    ) -> bool:
        """Atomically SET ``set_fields`` (+ the ordering timestamp) on the
        subscription, applied ONLY when this event's ``signedDate`` is >= the
        stored ``last_notification_signed_date_ms`` (advance-only).

        This is the single safe way to mutate a subscription from a webhook:
        - TARGETED update (never a full put) so it can't clobber other fields
          that a concurrent handler just wrote,
        - CONDITIONAL on signedDate so a retried/out-of-order Apple notification
          can't overwrite newer state (Apple retries and does NOT guarantee
          order),
        so the newest event always wins regardless of delivery order, GSI read
        staleness, or Lambda concurrency.

        Returns True if applied, False if skipped (stale/out-of-order). Logs a
        subscription event on apply. ``set_fields`` keys are stored attribute
        names, each aliased via ExpressionAttributeNames so DynamoDB reserved
        words (e.g. ``status``) are safe.
        """
        key = {
            "user_id": subscription.user_id,
            "subscription_id": subscription.subscription_id,
        }
        parts: List[str] = []
        values: Dict[str, Any] = {}
        names: Dict[str, str] = {}
        for i, (field, val) in enumerate(set_fields.items()):
            nm, ph = f"#f{i}", f":v{i}"
            names[nm] = field
            values[ph] = val
            parts.append(f"{nm} = {ph}")

        condition = None
        if signed_ms is not None:
            values[":sd"] = signed_ms
            parts.append("last_notification_signed_date_ms = :sd")
            condition = (
                "attribute_not_exists(last_notification_signed_date_ms) "
                "OR last_notification_signed_date_ms <= :sd"
            )

        applied = await self.dynamodb_service.update_item(
            self.subscriptions_table,
            key,
            "SET " + ", ".join(parts),
            values,
            expression_names=names,
            condition_expression=condition,
        )
        if not applied:
            logger.info(
                "Skipped stale/out-of-order %s for subscription %s (signedDate=%s)",
                event_type,
                subscription.subscription_id,
                signed_ms,
            )
            return False

        # Reflect applied fields on the in-memory object so downstream profile
        # logic (e.g. _update_user_subscription_status) sees the new values.
        for field, val in set_fields.items():
            setattr(subscription, field, val)

        await self._log_subscription_event(
            user_id=subscription.user_id,
            subscription_id=subscription.subscription_id,
            event_type=event_type,
            platform=subscription.platform.value,
            metadata={"signed_date_ms": signed_ms},
        )
        return True

    async def _revoke_profile_access(self, subscription: "Subscription") -> None:
        """Apply the profile-side effect of a subscription ENDING (expired /
        refunded). Core and the storage add-on end differently:

        - STORAGE add-on ends -> drop only the add-on overlay
          (storage_add_on_active=False, storage_subscription_id=None); Core
          access is kept. Quota falls 150 -> 50.
        - CORE ends (and the user has no OTHER active Core) -> clear Core AND the
          add-on: the add-on requires Core, so ending Core ends the add-on too.
          Access is revoked (quota -> 0).

        tier + quota are always re-derived via recompute_entitlement()."""
        user_profile = await self.dynamodb_service.get_user_profile(
            subscription.user_id
        )
        if not user_profile:
            return

        if subscription.subscription_type == SubscriptionType.STORAGE_ADD_ON:
            # Only clear if THIS is the tracked add-on (ignore a stale/older row).
            if user_profile.storage_subscription_id in (
                None,
                subscription.subscription_id,
            ):
                user_profile.storage_add_on_active = False
                user_profile.storage_subscription_id = None
        else:
            # Core (or unknown type) is ending. Keep access only if another Core
            # subscription is still active — a still-active add-on does NOT keep
            # Core access.
            user_subscriptions = await self.dynamodb_service.query_items(
                table_name=self.subscriptions_table,
                key_condition="user_id = :uid",
                expression_values={":uid": subscription.user_id},
            )
            has_other_active_core = any(
                sub.get("status") in ("active", "trial")
                and sub.get("subscription_type") == SubscriptionType.MIRROR_CORE.value
                for sub in user_subscriptions
                if sub.get("subscription_id") != subscription.subscription_id
            )
            if has_other_active_core:
                return
            user_profile.subscription_status = "expired"
            user_profile.primary_subscription_id = None
            # The add-on requires Core — ending Core ends the add-on too.
            user_profile.storage_add_on_active = False
            user_profile.storage_subscription_id = None

        user_profile.recompute_entitlement()
        await self.dynamodb_service.update_user_profile(user_profile)

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
            # Prefer originalTransactionId — it's stable across renewals and is
            # the key subscriptions are stored under. transactionId changes on
            # every renewal, so using it first would orphan renewal/cancel/
            # expiry notifications (the record would never be found).
            transaction_id = transaction_info.get(
                "originalTransactionId"
            ) or transaction_info.get("transactionId")
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

            # Compute the renewed expiry (Apple: expiresDate ms; Google:
            # expiryTimeMillis).
            expiry_iso = None
            if transaction_info.get("expiresDate"):
                expiry_iso = _ms_to_iso(transaction_info["expiresDate"])
            elif transaction_info.get("expiryTimeMillis"):
                expiry_iso = _ms_to_iso(transaction_info["expiryTimeMillis"])

            # A paid renewal is no longer a trial/intro period unless the renewal
            # transaction itself carries an intro offerType (1=free-trial, 2/3=
            # intro) — otherwise a converted user stays wrongly classified as
            # "trial".
            fields: Dict[str, Any] = {
                "status": SubscriptionStatus.ACTIVE.value,
                "is_in_trial": transaction_info.get("offerType") in (1, 2, 3),
            }
            if expiry_iso:
                fields["expiry_date"] = expiry_iso

            applied = await self._ordered_subscription_set(
                subscription,
                self._event_signed_ms(transaction_info),
                fields,
                "renewed",
            )
            if applied:
                await self._update_user_subscription_status(
                    subscription.user_id, subscription
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
            # Prefer originalTransactionId — it's stable across renewals and is
            # the key subscriptions are stored under. transactionId changes on
            # every renewal, so using it first would orphan renewal/cancel/
            # expiry notifications (the record would never be found).
            transaction_id = transaction_info.get(
                "originalTransactionId"
            ) or transaction_info.get("transactionId")

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

            # Grace period — the user still has access while Apple retries billing.
            applied = await self._ordered_subscription_set(
                subscription,
                self._event_signed_ms(transaction_info),
                {"status": SubscriptionStatus.GRACE_PERIOD.value},
                "renewal_failed",
            )
            if applied:
                # TODO: send a push notification about the payment failure
                # (best-effort; integrate with the notification service).
                logger.info(
                    f"Should send payment failure notification to user {subscription.user_id}"
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
            # Prefer originalTransactionId — it's stable across renewals and is
            # the key subscriptions are stored under. transactionId changes on
            # every renewal, so using it first would orphan renewal/cancel/
            # expiry notifications (the record would never be found).
            transaction_id = transaction_info.get(
                "originalTransactionId"
            ) or transaction_info.get("transactionId")

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

            applied = await self._ordered_subscription_set(
                subscription,
                self._event_signed_ms(transaction_info),
                {
                    "status": SubscriptionStatus.EXPIRED.value,
                    "auto_renew_enabled": False,
                },
                "expired",
            )
            if applied:
                await self._revoke_profile_access(subscription)

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
            # Prefer originalTransactionId — it's stable across renewals and is
            # the key subscriptions are stored under. transactionId changes on
            # every renewal, so using it first would orphan renewal/cancel/
            # expiry notifications (the record would never be found).
            transaction_id = transaction_info.get(
                "originalTransactionId"
            ) or transaction_info.get("transactionId")

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

            applied = await self._ordered_subscription_set(
                subscription,
                self._event_signed_ms(transaction_info),
                {
                    "status": SubscriptionStatus.REFUNDED.value,
                    "auto_renew_enabled": False,
                },
                "refunded",
            )
            if applied:
                # Refunds require immediate access removal.
                await self._revoke_profile_access(subscription)

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
            # Prefer originalTransactionId — it's stable across renewals and is
            # the key subscriptions are stored under. transactionId changes on
            # every renewal, so using it first would orphan renewal/cancel/
            # expiry notifications (the record would never be found).
            transaction_id = transaction_info.get(
                "originalTransactionId"
            ) or transaction_info.get("transactionId")
            # NB: use an explicit None check, not ``or`` — autoRenewStatus is 0
            # when auto-renew is OFF (a cancellation), and ``0 or ...`` would
            # discard it and drop the cancel.
            auto_renew_status = transaction_info.get("autoRenewStatus")
            if auto_renew_status is None:
                auto_renew_status = transaction_info.get("autoRenewing")

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

            # Apply the auto-renew flip via the shared atomic, signedDate-ordered
            # write (see _ordered_subscription_set) so a retried/out-of-order
            # DID_CHANGE_RENEWAL_STATUS can't clobber newer state.
            if auto_renew_status is not None:
                # Apple sends 0/1 (int or IntEnum) or "0"/"1"; Google sends bool.
                if isinstance(auto_renew_status, str):
                    new_auto = auto_renew_status == "1"
                else:
                    new_auto = bool(auto_renew_status)

                applied = await self._ordered_subscription_set(
                    subscription,
                    self._event_signed_ms(transaction_info),
                    {"auto_renew_enabled": new_auto},
                    "auto_renew_status_changed",
                )
                if applied:
                    logger.info(
                        "Successfully processed renewal status change for "
                        f"subscription {subscription.subscription_id}: "
                        f"auto_renew={new_auto}"
                    )

        except Exception as e:
            logger.error(f"Error handling renewal status change: {e}", exc_info=True)
