"""
Scheduled hourly job — dispatch Soul Pings.

Runs every hour (EventBridge cron in serverless.yml). For each user with an
active device token it attempts one Soul Ping: the SoulPingService enforces the
per-user one-per-hour throttle, the user's enabled-categories config, and skips
users with no usable conversation history. So this job naturally sends at most
one ping per user per hour.

All per-user work is isolated — one user's failure never blocks the rest.
"""

import asyncio
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List

from ..services.dynamodb_service import get_dynamodb_service
from ..services.soul_ping_service import PingResult, get_soul_ping_service

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Bound concurrent per-user pipelines (each does an LLM call + a few Dynamo/SNS
# round-trips) so a large user base can't fan out unbounded inside one Lambda.
CONCURRENCY = int(os.getenv("SOUL_PING_CONCURRENCY", "8"))


async def run_soul_ping_dispatch() -> Dict[str, Any]:
    db = get_dynamodb_service()
    service = get_soul_ping_service()

    user_ids = await db.scan_active_device_user_ids()
    logger.info(f"Soul Ping dispatch: {len(user_ids)} candidate users")

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def _one(user_id: str) -> PingResult:
        async with semaphore:
            try:
                return await service.maybe_send_for_user(user_id)
            except Exception as e:  # noqa: BLE001 - isolate per-user failures
                logger.error(f"Soul ping failed for {user_id}: {e}", exc_info=True)
                return PingResult(user_id, "skipped", "error")

    results: List[PingResult] = await asyncio.gather(*[_one(uid) for uid in user_ids])

    sent = [r for r in results if r.status == "sent"]
    reasons = Counter(r.reason for r in results if r.status == "skipped")
    summary = {
        "candidates": len(user_ids),
        "sent": len(sent),
        "skipped": len(results) - len(sent),
        "skip_reasons": dict(reasons),
        "categories_sent": dict(Counter(r.category for r in sent)),
    }
    logger.info(f"Soul Ping dispatch complete: {summary}")
    return summary


def lambda_handler(event: Dict, context: Any) -> Dict:
    """EventBridge-triggered handler (hourly). See serverless.yml soulPingDispatch."""
    logger.info(f"Soul Ping dispatch triggered by: {event.get('source', 'manual')}")
    try:
        summary = asyncio.run(run_soul_ping_dispatch())
        return {
            "statusCode": 200,
            "body": {
                "success": True,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "results": summary,
            },
        }
    except Exception as e:  # noqa: BLE001
        logger.error(f"Soul Ping dispatch job error: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": {"success": False, "error": str(e)},
        }


if __name__ == "__main__":
    print("Running Soul Ping dispatch locally...")
    print(asyncio.run(run_soul_ping_dispatch()))
