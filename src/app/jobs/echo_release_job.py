"""
Scheduled job: auto-release DRAFT echoes whose release_date has passed.

Triggered hourly by AWS EventBridge. Delegates the actual scan + release
work to `EchoService.release_due_echoes`; this module's only job is the
Lambda boilerplate (async runner, structured response, telemetry log).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from ..services.echo_service import get_echo_service

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


async def release_due_echoes() -> Dict[str, Any]:
    """
    Scan for DRAFT echoes whose release_date has passed and release each.

    Returns:
        Dict with job execution results — `success`, `timestamp`, and the
        breakdown from EchoService.release_due_echoes (scanned/released/
        skipped/failed/errors).
    """
    try:
        echo_service = get_echo_service()
        logger.info("Starting echo auto-release scan")

        result = await echo_service.release_due_echoes()

        logger.info(
            f"Echo auto-release scan complete: "
            f"scanned={result.get('scanned', 0)}, "
            f"released={result.get('released', 0)}, "
            f"skipped={result.get('skipped', 0)}, "
            f"failed={result.get('failed', 0)}"
        )

        return {
            "success": True,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "results": result,
        }

    except Exception as e:
        logger.error(f"Error in echo auto-release job: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    AWS Lambda handler for hourly scheduled echo releases.

    Triggered by EventBridge (CloudWatch Events) on a cron schedule defined
    in serverless.yml (`cron(0 * * * ? *)` — every hour at minute 0 UTC).

    Args:
        event: EventBridge event payload
        context: Lambda context

    Returns:
        Dict with HTTP-style statusCode and the job result body.
    """
    import asyncio

    logger.info(f"Echo auto-release job triggered by: {event.get('source', 'manual')}")

    result = asyncio.run(release_due_echoes())

    return {
        "statusCode": 200 if result["success"] else 500,
        "body": result,
    }


# For local testing
if __name__ == "__main__":
    import asyncio

    print("Running echo auto-release scan locally...")
    result = asyncio.run(release_due_echoes())
    print(f"Result: {result}")
