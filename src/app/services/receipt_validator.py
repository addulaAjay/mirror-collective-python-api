"""
Receipt validation service for Apple and Google IAP.

Apple flow (Phase A, 2026-05-11):
  - Replaced the deprecated `verifyReceipt` endpoint with the App Store
    Server API (`get_all_subscription_statuses`) keyed on
    `originalTransactionId`.
  - All returned transactions are JWS x5c-verified against the
    bundled Apple Root CA - G3 via SignedDataVerifier.
  - See src/app/services/apple_app_store_client.py for the SDK wrapper.

Google flow:
  - Unchanged. Uses the Google Play Developer API with a service-account
    credential (`GOOGLE_SERVICE_ACCOUNT_KEY`, `GOOGLE_PACKAGE_NAME`).
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _iso_from_millis(millis_value) -> Optional[str]:
    """Convert an epoch-millisecond integer to an ISO 8601 UTC string.

    Returns None on missing / unparseable input. Matches the Z-suffix
    convention used elsewhere in the codebase.
    """
    if millis_value is None:
        return None
    try:
        seconds = float(millis_value) / 1000.0
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        return dt.isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


class ReceiptValidator:
    """
    Validate IAP receipts with Apple's App Store Server API and Google
    Play's Developer API.
    """

    # Google endpoint kept here for reference; the actual call goes
    # through the google-api-python-client below.
    GOOGLE_API_URL = "https://androidpublisher.googleapis.com/androidpublisher/v3"

    def __init__(self):
        self.google_service_account_key = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY")
        self.google_package_name = os.getenv(
            "GOOGLE_PACKAGE_NAME", "com.themirrorcollective.mirror"
        )

    async def validate_apple_receipt(
        self,
        receipt_data: str,
        original_transaction_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validate an iOS purchase via the App Store Server API.

        Phase A replaces the legacy `verifyReceipt` flow. Instead of
        POSTing a base64 receipt blob to Apple, we use the
        `originalTransactionId` (captured client-side after a successful
        StoreKit purchase) to fetch the authoritative subscription
        status and verify the returned JWS payloads via the bundled
        SignedDataVerifier.

        Args:
            receipt_data: Legacy field — accepted for backwards compat
                with clients that haven't migrated to sending
                `original_transaction_id` yet. If `original_transaction_id`
                is not provided, this value is used as-is on the
                assumption that the client put the transaction id here.
            original_transaction_id: Apple's `originalTransactionId` for
                the subscription (StoreKit 2 canonical id). Always send
                this on newer clients; `receipt_data` is the fallback.

        Returns:
            Dict with {"valid": bool, "data": dict|None, "error": str|None}.
            On success, `data` matches the legacy parsed shape used by
            subscription_service.verify_and_activate_purchase.
        """
        from appstoreserverlibrary.api_client import APIException

        from .apple_app_store_client import (
            AppleClientConfigError,
            AppleSignatureVerificationError,
        )

        txn_id = original_transaction_id or receipt_data
        if not txn_id:
            return {
                "valid": False,
                "data": None,
                "error": "Apple validation requires an original_transaction_id.",
            }

        try:
            from . import apple_app_store_client

            result = apple_app_store_client.get_subscription_statuses(txn_id)
        except AppleClientConfigError as exc:
            logger.error("Apple App Store client misconfigured: %s", exc)
            return {"valid": False, "data": None, "error": str(exc)}
        except AppleSignatureVerificationError as exc:
            logger.warning(
                "Apple JWS verification failed for transaction %s: %s", txn_id, exc
            )
            return {
                "valid": False,
                "data": None,
                "error": f"Receipt signature verification failed: {exc}",
            }
        except APIException as exc:
            logger.error("App Store Server API call failed for %s: %s", txn_id, exc)
            return {
                "valid": False,
                "data": None,
                "error": f"App Store Server API error: {exc}",
            }

        latest = result.get("latest_signed_transaction")
        if not latest:
            return {
                "valid": False,
                "data": None,
                "error": "No signed transaction found for this id.",
            }

        return {
            "valid": True,
            "data": self.parse_apple_signed_transaction(latest, result),
            "error": None,
        }

    async def validate_google_receipt(
        self, receipt_data: str, product_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Validate Android receipt with Google Play API

        Args:
            receipt_data: Purchase token from Google Play
            product_id: Product identifier (subscription SKU)

        Returns:
            Dict with validation result: {"valid": bool, "data": dict, "error": str}
        """
        try:
            package_name = os.getenv("GOOGLE_PACKAGE_NAME")
            service_account_key_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY")

            if not package_name:
                logger.error("Google Play package name not configured")
                return {
                    "valid": False,
                    "error": "Google Play validation not configured. Set GOOGLE_PACKAGE_NAME environment variable.",
                    "data": None,
                }

            if not service_account_key_path:
                logger.error("Google service account key not configured")
                return {
                    "valid": False,
                    "error": "Google Play validation not configured. Set GOOGLE_SERVICE_ACCOUNT_KEY environment variable.",
                    "data": None,
                }

            if not product_id:
                logger.error("Product ID required for Google validation")
                return {"valid": False, "error": "Product ID required", "data": None}

            # Import Google libraries
            try:
                from google.oauth2 import service_account
                from googleapiclient.discovery import build
            except ImportError:
                logger.error(
                    "Google libraries not installed. Install: pip install google-auth google-api-python-client"
                )
                return {
                    "valid": False,
                    "error": "Google Play validation requires google-auth and google-api-python-client. "
                    "Install: pip install google-auth google-api-python-client",
                    "data": None,
                }

            # Load service account credentials from JSON file
            try:
                credentials = service_account.Credentials.from_service_account_file(
                    service_account_key_path,
                    scopes=["https://www.googleapis.com/auth/androidpublisher"],
                )
            except Exception as e:
                logger.error("Failed to load service account credentials: %s", e)
                return {
                    "valid": False,
                    "error": f"Failed to load service account credentials: {str(e)}",
                    "data": None,
                }

            # Build Google Play Developer API client
            try:
                service = build("androidpublisher", "v3", credentials=credentials)
            except Exception as e:
                logger.error("Failed to build Google Play API client: %s", e)
                return {
                    "valid": False,
                    "error": f"Failed to build Google Play API client: {str(e)}",
                    "data": None,
                }

            # Validate subscription purchase
            try:
                result = (
                    service.purchases()
                    .subscriptions()
                    .get(
                        packageName=package_name,
                        subscriptionId=product_id,
                        token=receipt_data,
                    )
                    .execute()
                )

                # Check payment state (0=pending, 1=received, 2=free trial, 3=pending deferred upgrade/downgrade)
                payment_state = result.get("paymentState")
                if payment_state not in [1, 2]:
                    return {
                        "valid": False,
                        "error": f"Invalid payment state: {payment_state}",
                        "data": None,
                    }

                # Parse and return subscription data. The legacy
                # `purchases.subscriptions` endpoint doesn't echo the
                # productId in its response, so inject the value the
                # caller passed in so the downstream cross-check has
                # something authoritative to compare against.
                parsed_data = self.parse_google_purchase(result)
                if product_id and not parsed_data.get("product_id"):
                    parsed_data["product_id"] = product_id
                return {"valid": True, "data": parsed_data, "error": None}

            except Exception as e:
                logger.error("Google Play API error: %s", e)
                return {
                    "valid": False,
                    "error": f"Google Play API error: {str(e)}",
                    "data": None,
                }

        except Exception as e:
            logger.error("Error validating Google receipt: %s", e)
            return {"valid": False, "error": str(e), "data": None}

    def parse_apple_signed_transaction(
        self,
        transaction: Dict[str, Any],
        status_response: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Map a verified Apple JWS transaction payload to the legacy
        parsed shape used by subscription_service.

        The SDK returns a `JWSTransactionDecodedPayload` (as dict here)
        with camelCase fields and epoch-second timestamps. Downstream
        code expects snake_case fields and epoch-millisecond strings —
        see legacy `parse_apple_receipt` for the contract.

        Args:
            transaction: Decoded transaction payload from the SDK.
            status_response: Optional full subscription-status response
                (provides auto_renew_status from the pendingRenewalInfo).
        """
        try:
            # Resolve auto_renew_enabled from the JWS-verified renewal info
            # that apple_app_store_client now attaches under "_renewal_info"
            # on each transaction. autoRenewStatus is the SDK's enum
            # (0 = off, 1 = on). Fallback default = True only when we
            # have no signal at all, which happens for transactions that
            # arrived via a path that doesn't carry renewal info (e.g.
            # legacy receipts during the migration window).
            renewal_info = transaction.get("_renewal_info") or {}
            renewal_status = renewal_info.get("autoRenewStatus")
            if renewal_status is None:
                auto_renew = True
            else:
                try:
                    auto_renew = int(renewal_status) == 1
                except (TypeError, ValueError):
                    # Some payloads serialise the enum as a string;
                    # fall back to truthiness.
                    auto_renew = bool(renewal_status)

            offer_type = transaction.get("offerType") or transaction.get("offer_type")
            # offer_type=1 is INTRODUCTORY in StoreKit 2 enum semantics.
            # SDK returns the enum value; coerce to int safely.
            try:
                is_intro = int(offer_type) == 1 if offer_type is not None else False
            except (TypeError, ValueError):
                is_intro = False

            # Trial: SDK uses `type` of "Auto-Renewable Subscription"
            # plus `offerType=1` to signal an intro/trial. The
            # `is_trial_period` flag the old code consumed isn't a
            # direct SDK field, so we approximate from offer_type.
            is_trial = bool(transaction.get("isTrialPeriod") or is_intro)

            # SDK price is in micros (e.g. 15990000 == $15.99); convert
            # to a float. Currency is exposed separately.
            sdk_price = transaction.get("price")
            try:
                price_usd = (
                    float(sdk_price) / 1_000_000.0 if sdk_price is not None else 0.0
                )
            except (TypeError, ValueError):
                price_usd = 0.0
            currency_code = (
                transaction.get("currency") or transaction.get("currencyCode") or "USD"
            )

            return {
                # Field names matching the Subscription model expectations.
                "transaction_id": transaction.get("transactionId")
                or transaction.get("transaction_id"),
                "original_transaction_id": transaction.get("originalTransactionId")
                or transaction.get("original_transaction_id"),
                "product_id": transaction.get("productId")
                or transaction.get("product_id"),
                "purchase_date": _iso_from_millis(
                    transaction.get("purchaseDate") or transaction.get("purchase_date")
                ),
                "expiry_date": _iso_from_millis(
                    transaction.get("expiresDate") or transaction.get("expires_date")
                ),
                "cancellation_date": _iso_from_millis(
                    transaction.get("revocationDate")
                    or transaction.get("revocation_date")
                ),
                "is_trial_period": is_trial,
                "is_in_intro_offer_period": is_intro,
                "auto_renew_enabled": auto_renew,
                "price": price_usd,
                "currency_code": currency_code,
                "environment": transaction.get("environment"),
                "bundle_id": transaction.get("bundleId")
                or transaction.get("bundle_id"),
            }

        except Exception as e:
            logger.error("Error parsing Apple signed transaction: %s", e)
            return {}

    def parse_google_purchase(self, purchase_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract relevant subscription data from Google purchase

        Args:
            purchase_info: Purchase info from Google Play API

        Returns:
            Dict with parsed subscription data
        """
        try:
            # Google's API returns the subscription product as a field
            # called `productId` ONLY when querying by purchase token via
            # the v1 / v2 endpoints. The legacy `purchases.subscriptions`
            # response we use omits it (the caller already knows it from
            # the request), so we accept either. The `product_id` kwarg
            # path goes through subscription_service which passes the
            # client-claimed product through validate_google_receipt.
            order_id = purchase_info.get("orderId")
            return {
                # Canonical idempotency key matching the Apple shape.
                # On Google, the orderId IS the original transaction
                # identifier for the subscription's lifetime.
                "transaction_id": order_id,
                "original_transaction_id": order_id,
                "product_id": purchase_info.get("productId"),
                # ISO timestamps to match the Apple parser + Subscription
                # model contract. _iso_from_millis is defined at module
                # top and handles missing / unparseable inputs.
                "purchase_date": _iso_from_millis(purchase_info.get("startTimeMillis")),
                "expiry_date": _iso_from_millis(purchase_info.get("expiryTimeMillis")),
                "auto_renew_enabled": bool(purchase_info.get("autoRenewing", False)),
                # Google has no explicit "trial period" flag; paymentState=2
                # means free-trial-in-progress (per Google's docs).
                "is_trial_period": purchase_info.get("paymentState") == 2,
                "is_in_intro_offer_period": purchase_info.get("paymentState") == 2,
                # No price / currency exposed by this Google endpoint —
                # downstream code defaults price to 0.0 which is fine
                # since SKU + billing_period are the truth source.
                "price": 0.0,
                "currency_code": purchase_info.get("priceCurrencyCode", "USD"),
                "environment": (
                    "sandbox"
                    if purchase_info.get("purchaseType") == 0
                    else "production"
                ),
            }

        except Exception as e:
            logger.error("Error parsing Google purchase: %s", e)
            return {}
