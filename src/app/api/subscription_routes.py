"""
Subscription API routes for trial management and IAP
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..core.enhanced_auth import get_user_with_profile
from ..services.dynamodb_service import DynamoDBService
from ..services.storage_quota_service import StorageQuotaService
from ..services.subscription_service import SubscriptionService
from ..services.trial_management_service import TrialManagementService

# Initialize router
router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])

# Initialize services
dynamodb_service = DynamoDBService()
trial_service = TrialManagementService(dynamodb_service)
quota_service = StorageQuotaService(dynamodb_service)
subscription_service = SubscriptionService(dynamodb_service)


# Request/Response Models
class VerifyPurchaseRequest(BaseModel):
    """Body of POST /verify-purchase. The client sends this after a
    successful StoreKit / BillingClient transaction so the backend can
    independently verify the receipt with Apple / Google and activate
    the subscription on this user's account."""

    platform: str  # "ios" | "android"
    # iOS: kept for backwards compat. New clients should send
    # `original_transaction_id` instead — the App Store Server API
    # flow no longer needs the legacy base64 receipt blob.
    # Android: the purchase token from Google Play Billing.
    receipt_data: str
    product_id: str
    # Apple's `originalTransactionId` (iOS) or Google's order base
    # (Android). Stable across renewals — exactly what we want as the
    # idempotency key. Required on new clients; older clients that
    # only send `transaction_id` are still accepted via the fallback
    # in verify_and_activate_purchase, but the rename is permanent.
    original_transaction_id: Optional[str] = None
    # Current transaction id from the SDK. On iOS this is the same as
    # original_transaction_id for first purchases but a fresh id per
    # renewal — useful for analytics, not for idempotency.
    transaction_id: str


class StartTrialRequest(BaseModel):
    """
    Body of POST /start-trial.

    Deprecated 2026-05-11 — replaced by Apple/Google native intro
    offers configured on each subscription product. The native intro
    offer is presented automatically by the OS during the StoreKit /
    BillingClient flow, so a separate "I want to start a trial"
    server call is no longer needed.

    The endpoint and this schema are kept for older clients that still
    call it during onboarding. Once frontend telemetry shows zero calls
    over a release cycle, remove both.
    """

    # Optional client metadata for analytics — none of it gates the
    # trial start.
    device_id: Optional[str] = None
    app_version: Optional[str] = None


class CancelSubscriptionRequest(BaseModel):
    subscription_id: str


# Trial Management Endpoints


@router.post("/start-trial", deprecated=True)
async def start_trial(
    request: Optional[StartTrialRequest] = None,
    current_user: Dict = Depends(get_user_with_profile),
):
    """
    [DEPRECATED — 2026-05-11]
    Start a 14-day free trial via the legacy no-payment flow.

    Use the App Store / Play Store native intro offer instead — the
    OS-native subscription sheet presents the trial automatically when
    the user taps START FREE TRIAL on the client. See
    docs/IAP_STORE_SETUP.md §A2 and §B2 for product configuration.

    This endpoint will be removed once frontend telemetry confirms zero
    callers in a full release cycle.
    """
    try:
        user_id = current_user["id"]
        result = await trial_service.start_user_trial(user_id)

        return {
            "success": True,
            "message": "Free trial started successfully",
            "data": result,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start trial: {str(e)}")


@router.get("/trial-status")
async def get_trial_status(current_user: Dict = Depends(get_user_with_profile)):
    """
    Get trial status for current user

    Returns trial availability, status, and days remaining
    """
    try:
        user_id = current_user["id"]
        status = await trial_service.get_trial_status(user_id)

        return {"success": True, "data": status}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get trial status: {str(e)}"
        )


# Subscription Status


@router.get("/status")
async def get_subscription_status(
    current_user: Dict = Depends(get_user_with_profile),
):
    """
    Get comprehensive subscription status for user

    Returns tier, status, trial info, quotas, and features
    """
    try:
        user_id = current_user["id"]

        # Get user profile
        user_profile = await dynamodb_service.get_user_profile(user_id)
        if not user_profile:
            raise HTTPException(status_code=404, detail="User not found")

        # Calculate trial days remaining
        trial_days_remaining = 0
        if (
            user_profile.trial_expires_at
            and user_profile.subscription_status == "trial"
        ):
            from datetime import datetime, timezone

            expires = datetime.fromisoformat(
                user_profile.trial_expires_at.replace("Z", "+00:00")
            )
            now = datetime.now(timezone.utc)
            if expires > now:
                trial_days_remaining = (expires - now).days

        # Determine features based on tier
        features = {
            "echo_vault_enabled": user_profile.subscription_tier
            in ["trial", "core", "core_plus"],
            "quota_gb": user_profile.echo_vault_quota_gb,
            "used_gb": user_profile.echo_vault_used_gb,
            "mirror_gpt_enabled": True,  # Always enabled
            "echo_map_enabled": user_profile.subscription_tier
            in ["trial", "core", "core_plus"],
        }

        return {
            "success": True,
            "data": {
                "tier": user_profile.subscription_tier,
                "status": user_profile.subscription_status,
                "trial_days_remaining": trial_days_remaining,
                "features": features,
                "core_subscription": None,  # TODO: Fetch from subscriptions table
                "storage_subscription": None,  # TODO: Fetch from subscriptions table
                "has_used_trial": user_profile.has_used_trial,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get subscription status: {str(e)}"
        )


# Purchase Verification (placeholder for now)


@router.post("/verify-purchase")
async def verify_purchase(
    request: VerifyPurchaseRequest,
    current_user: Dict = Depends(get_user_with_profile),
):
    """
    Verify IAP receipt and activate subscription

    Validates receipt with Apple/Google and activates subscription
    """
    try:
        user_id = current_user["id"]

        # Prefer the stable original_transaction_id; fall back to the
        # legacy transaction_id field for older clients that haven't
        # been updated yet.
        lookup_id = request.original_transaction_id or request.transaction_id

        result = await subscription_service.verify_and_activate_purchase(
            user_id=user_id,
            platform=request.platform,
            receipt_data=request.receipt_data,
            product_id=request.product_id,
            transaction_id=lookup_id,
        )

        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to verify purchase: {str(e)}"
        )


class RestorePurchasesRequest(BaseModel):
    platform: str  # "ios" | "android"
    receipts: list  # List of receipt data strings


@router.post("/restore-purchases")
async def restore_purchases(
    request: RestorePurchasesRequest,
    current_user: Dict = Depends(get_user_with_profile),
):
    """
    Restore purchases from App Store/Play Store

    Validates all receipts and syncs subscription state
    """
    try:
        user_id = current_user["id"]

        result = await subscription_service.restore_user_purchases(
            user_id=user_id, platform=request.platform, receipts=request.receipts
        )

        return result
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to restore purchases: {str(e)}"
        )


# Storage Quota


@router.get("/quota-status")
async def get_quota_status(current_user: Dict = Depends(get_user_with_profile)):
    """
    Get storage quota status for user

    Returns usage, quota, percent used, and limit warnings
    """
    try:
        user_id = current_user["id"]
        quota_status = await quota_service.check_quota_exceeded(user_id)

        return {"success": True, "data": quota_status}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get quota status: {str(e)}"
        )


# Webhooks (placeholders)


@router.post("/webhook/apple")
async def apple_webhook(request: Request):
    """
    Apple App Store Server Notifications V2 webhook.

    Verifies the outer signedPayload and inner signedTransactionInfo
    as JWS x5c against Apple's bundled root CA before dispatching to
    any subscription lifecycle handler. Forged or unsigned payloads
    return 401 — they never reach business logic.
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")

    try:
        result = await subscription_service.handle_apple_webhook(body)
    except Exception as exc:
        # InternalServerError or other unexpected failure inside the
        # lifecycle handlers — surface as 500.
        raise HTTPException(status_code=500, detail=str(exc))

    # The service signals signature / payload errors via a
    # `status_code` field (401/400). Honour it so Apple sees the right
    # response and stops retrying when appropriate.
    if not result.get("success"):
        status_code = result.get("status_code", 400)
        raise HTTPException(
            status_code=status_code, detail=result.get("error", "Webhook rejected")
        )

    return result


@router.post("/webhook/google")
async def google_webhook(request: Request):
    """
    Google Play Real-time Developer Notifications webhook.

    The Pub/Sub OIDC JWT (Authorization: Bearer ...) is verified
    against Google's published certs by subscription_service before
    the inner notification payload is dispatched. See
    `_verify_google_pubsub_message` and `verify_pubsub_jwt`.
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}")

    # Forward the Authorization header so the service can verify the
    # OIDC JWT Google attaches to each push delivery.
    auth_header = request.headers.get("authorization") or request.headers.get(
        "Authorization"
    )

    try:
        result = await subscription_service.handle_google_webhook(
            body, auth_header=auth_header
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    if not result.get("success"):
        status_code = result.get("status_code", 400)
        raise HTTPException(
            status_code=status_code, detail=result.get("error", "Webhook rejected")
        )

    return result


# Subscription Management


@router.post("/cancel")
async def cancel_subscription(
    request: CancelSubscriptionRequest,
    current_user: Dict = Depends(get_user_with_profile),
):
    """
    Cancel subscription auto-renewal

    User retains access until current period expires
    """
    try:
        user_id = current_user["id"]

        result = await subscription_service.cancel_subscription(
            user_id=user_id, subscription_id=request.subscription_id
        )

        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to cancel subscription: {str(e)}"
        )


@router.get("/billing-history")
async def get_billing_history(current_user: Dict = Depends(get_user_with_profile)):
    """
    Get billing and event history for user

    Returns subscription events and transaction history
    """
    try:
        user_id = current_user["id"]

        result = await subscription_service.get_billing_history(user_id)

        return result
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to get billing history: {str(e)}"
        )
