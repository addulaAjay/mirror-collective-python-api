"""
Scheduled job to reconcile subscription state against Apple.

This Lambda runs daily as the belt-and-suspenders backstop for missed or
delayed App Store Server Notifications. For every subscription still in an
active-ish state it fetches Apple's authoritative status (Get All Subscription
Statuses) and corrects DB drift:

1. Lapsed subs (expired/revoked) that never received their EXPIRED/REFUND
   webhook -> marked expired/refunded and access revoked.
2. Subs in billing retry / grace -> marked grace_period (access kept).
3. Healthy subs with a stale expiry_date -> expiry advanced to Apple's latest
   verified renewal (the drift that otherwise wrongly 403s an entitled user).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from ..services.dynamodb_service import get_dynamodb_service
from ..services.subscription_service import SubscriptionService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


async def reconcile_subscriptions() -> Dict[str, Any]:
    """
    Reconcile all active-ish subscriptions against Apple.

    Returns:
        Dict with job execution results
    """
    try:
        dynamodb_service = get_dynamodb_service()
        subscription_service = SubscriptionService(dynamodb_service)

        logger.info("Starting subscription reconciliation job")

        result = await subscription_service.reconcile_subscriptions()

        logger.info(
            f"Subscription reconciliation complete: "
            f"{result.get('checked', 0)} checked, "
            f"{result.get('corrected', 0)} corrected, "
            f"{result.get('errors', 0)} errors"
        )

        return {
            "success": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "results": result,
        }

    except Exception as e:
        logger.error(f"Error in subscription reconciliation job: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    AWS Lambda handler for scheduled subscription reconciliation.

    Triggered by AWS EventBridge (CloudWatch Events) on a daily schedule.

    Args:
        event: EventBridge event payload
        context: Lambda context

    Returns:
        Dict with job execution results
    """
    import asyncio

    logger.info(
        f"Subscription reconciliation job triggered by: {event.get('source', 'manual')}"
    )

    result = asyncio.run(reconcile_subscriptions())

    return {
        "statusCode": 200 if result["success"] else 500,
        "body": result,
    }


# For local testing
if __name__ == "__main__":
    import asyncio

    print("Running subscription reconciliation locally...")
    result = asyncio.run(reconcile_subscriptions())
    print(f"Result: {result}")
