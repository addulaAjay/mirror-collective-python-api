"""
Receipt validation service for Apple and Google IAP
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)


class ReceiptValidator:
    """
    Validate IAP receipts with Apple and Google servers
    """

    # Apple endpoints
    APPLE_PRODUCTION_URL = "https://buy.itunes.apple.com/verifyReceipt"
    APPLE_SANDBOX_URL = "https://sandbox.itunes.apple.com/verifyReceipt"

    # Google endpoints
    GOOGLE_API_URL = "https://androidpublisher.googleapis.com/androidpublisher/v3"

    def __init__(self):
        self.apple_shared_secret = os.getenv("APPLE_SHARED_SECRET")
        self.google_service_account_key = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY")
        self.google_package_name = os.getenv(
            "GOOGLE_PACKAGE_NAME", "com.mirrorcollective.app"
        )

    async def validate_apple_receipt(
        self, receipt_data: str, exclude_old_transactions: bool = True
    ) -> Dict:
        """
        Validate iOS receipt with Apple servers

        Args:
            receipt_data: Base64 encoded receipt
            exclude_old_transactions: Exclude old transaction data

        Returns:
            Dict with validation result: {"valid": bool, "data": dict, "error": str}
        """
        try:
            request_body = {
                "receipt-data": receipt_data,
                "password": self.apple_shared_secret,
                "exclude-old-transactions": exclude_old_transactions,
            }

            async with aiohttp.ClientSession() as session:
                # Try production first
                async with session.post(
                    self.APPLE_PRODUCTION_URL, json=request_body
                ) as response:
                    result = await response.json()

                    # Status 21007 = sandbox receipt sent to production
                    if result.get("status") == 21007:
                        # Retry with sandbox
                        async with session.post(
                            self.APPLE_SANDBOX_URL, json=request_body
                        ) as sandbox_response:
                            result = await sandbox_response.json()

                    # Status 0 = valid
                    if result.get("status") == 0:
                        # Parse receipt data
                        parsed_data = self.parse_apple_receipt(result)
                        return {"valid": True, "data": parsed_data, "error": None}

                    # Handle error codes
                    error_codes = {
                        21000: "Malformed request",
                        21002: "Invalid receipt data",
                        21003: "Receipt authentication failed",
                        21005: "Server unavailable",
                        21008: "Receipt from wrong environment",
                    }
                    error_msg = error_codes.get(
                        result.get("status"), f"Unknown error: {result.get('status')}"
                    )
                    return {"valid": False, "data": None, "error": error_msg}

        except Exception as e:
            logger.error(f"Error validating Apple receipt: {e}")
            return {"valid": False, "data": None, "error": str(e)}

    async def validate_google_receipt(
        self, receipt_data: str, product_id: Optional[str] = None
    ) -> Dict:
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
                logger.error(f"Failed to load service account credentials: {e}")
                return {
                    "valid": False,
                    "error": f"Failed to load service account credentials: {str(e)}",
                    "data": None,
                }

            # Build Google Play Developer API client
            try:
                service = build("androidpublisher", "v3", credentials=credentials)
            except Exception as e:
                logger.error(f"Failed to build Google Play API client: {e}")
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

                # Parse and return subscription data
                parsed_data = self.parse_google_purchase(result)
                return {"valid": True, "data": parsed_data, "error": None}

            except Exception as e:
                logger.error(f"Google Play API error: {e}")
                return {
                    "valid": False,
                    "error": f"Google Play API error: {str(e)}",
                    "data": None,
                }

        except Exception as e:
            logger.error(f"Error validating Google receipt: {e}")
            return {"valid": False, "error": str(e), "data": None}

    def parse_apple_receipt(self, receipt_info: Dict) -> Dict:
        """
        Extract relevant subscription data from Apple receipt

        Args:
            receipt_info: Full receipt response from Apple

        Returns:
            Dict with parsed subscription data
        """
        try:
            latest_receipt_info = receipt_info.get("latest_receipt_info", [])
            if not latest_receipt_info:
                return {}

            # Get most recent transaction
            latest = latest_receipt_info[-1]

            return {
                "transaction_id": latest.get("transaction_id"),
                "original_transaction_id": latest.get("original_transaction_id"),
                "product_id": latest.get("product_id"),
                "purchase_date_ms": latest.get("purchase_date_ms"),
                "expires_date_ms": latest.get("expires_date_ms"),
                "is_trial_period": latest.get("is_trial_period") == "true",
                "is_in_intro_offer_period": latest.get("is_in_intro_offer_period")
                == "true",
                "cancellation_date_ms": latest.get("cancellation_date_ms"),
                "auto_renew_status": receipt_info.get("pending_renewal_info", [{}])[
                    0
                ].get("auto_renew_status")
                == "1",
            }

        except Exception as e:
            logger.error(f"Error parsing Apple receipt: {e}")
            return {}

    def parse_google_purchase(self, purchase_info: Dict) -> Dict:
        """
        Extract relevant subscription data from Google purchase

        Args:
            purchase_info: Purchase info from Google Play API

        Returns:
            Dict with parsed subscription data
        """
        try:
            return {
                "order_id": purchase_info.get("orderId"),
                "product_id": purchase_info.get("productId"),
                "purchase_time_ms": purchase_info.get("startTimeMillis"),
                "expiry_time_ms": purchase_info.get("expiryTimeMillis"),
                "auto_renewing": purchase_info.get("autoRenewing", False),
                "payment_state": purchase_info.get(
                    "paymentState"
                ),  # 0=pending, 1=received
                "cancel_reason": purchase_info.get("cancelReason"),
                "user_cancellation_time_ms": purchase_info.get(
                    "userCancellationTimeMillis"
                ),
            }

        except Exception as e:
            logger.error(f"Error parsing Google purchase: {e}")
            return {}
