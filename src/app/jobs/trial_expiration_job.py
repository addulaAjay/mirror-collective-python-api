"""
Scheduled job to check trial expirations and send notifications

This Lambda function runs daily to:
1. Find all users with active trials
2. Check expiration dates
3. Send notifications at 7 days, 3 days, 1 day, and expiration
4. Lock Echo Vault access when trial expires
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from ..services.dynamodb_service import DynamoDBService
from ..services.trial_management_service import TrialManagementService

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


async def check_trial_expirations() -> Dict[str, Any]:
    """
    Check all active trials and send notifications

    Returns:
        Dict with job execution results
    """
    try:
        # Initialize services
        dynamodb_service = DynamoDBService()
        trial_service = TrialManagementService(dynamodb_service)

        logger.info("Starting trial expiration check job")

        # Check trial expirations (handles notifications and expiry)
        result = await trial_service.check_trial_expiration()

        logger.info(
            f"Trial expiration check complete: "
            f"{result.get('notifications_sent', 0)} notifications sent, "
            f"{result.get('trials_expired', 0)} trials expired"
        )

        return {
            "success": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "results": result,
        }

    except Exception as e:
        logger.error(f"Error in trial expiration job: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    AWS Lambda handler for scheduled trial expiration checks

    This function is triggered by AWS EventBridge (CloudWatch Events)
    on a daily schedule (cron: 0 12 * * ? *) - runs at 12:00 UTC daily

    Args:
        event: EventBridge event payload
        context: Lambda context

    Returns:
        Dict with job execution results
    """
    import asyncio

    logger.info(f"Trial expiration job triggered by: {event.get('source', 'manual')}")

    # Run async function
    result = asyncio.run(check_trial_expirations())

    return {
        "statusCode": 200 if result["success"] else 500,
        "body": result,
    }


# For local testing
if __name__ == "__main__":
    import asyncio

    print("Running trial expiration check locally...")
    result = asyncio.run(check_trial_expirations())
    print(f"Result: {result}")
