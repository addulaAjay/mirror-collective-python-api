"""
Subscription API routes for trial management and IAP
"""

from typing import Any, Dict

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
    platform: str  # "ios" | "android"
    receipt_data: str
    product_id: str
    transaction_id: str


class CancelSubscriptionRequest(BaseModel):
    subscription_id: str


# Trial Management Endpoints


@router.post("/start-trial")
async def start_trial(current_user: Dict = Depends(get_user_with_profile)):
    """
    Start 14-day free trial (no payment required)

    Returns trial start date, expiry date, and quota info
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

        result = await subscription_service.verify_and_activate_purchase(
            user_id=user_id,
            platform=request.platform,
            receipt_data=request.receipt_data,
            product_id=request.product_id,
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
    Apple App Store Server Notifications V2 webhook

    Processes subscription lifecycle events from Apple
    """
    try:
        body = await request.json()

        result = await subscription_service.handle_apple_webhook(body)

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/webhook/google")
async def google_webhook(request: Request):
    """
    Google Play Real-time Developer Notifications webhook

    Processes subscription lifecycle events from Google Play
    """
    try:
        body = await request.json()

        result = await subscription_service.handle_google_webhook(body)

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
