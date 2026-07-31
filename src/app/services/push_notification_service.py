"""Generic push-notification dispatch (device tokens → SNS).

This is the concrete implementation of the ``send_notification`` interface that
``TrialManagementService`` expects. Previously that service was constructed with
``push_service=None``, so trial-expiry nudges were silently dropped — this closes
that gap and mirrors the delivery path already used by Soul Ping.
"""

import logging
from typing import Any, Dict, List, Optional

from .dynamodb_service import DynamoDBService, get_dynamodb_service
from .sns_service import SNSService

logger = logging.getLogger(__name__)


class PushNotificationService:
    """Fan a single notification out to all of a user's active device endpoints.

    Kept intentionally small: it resolves the user's registered device tokens,
    filters to active SNS platform endpoints, and publishes to each. The caller
    supplies the lock-screen ``title``/``body`` and a machine-readable ``data``
    block (e.g. ``{"type": "trial_expired", "action": "upgrade"}``) the client
    uses to route to the paywall.
    """

    def __init__(
        self,
        dynamodb_service: Optional[DynamoDBService] = None,
        sns_service: Optional[SNSService] = None,
    ) -> None:
        self.db = dynamodb_service or get_dynamodb_service()
        self.sns = sns_service or SNSService()

    async def send_notification(
        self,
        user_id: str,
        title: str,
        body: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Deliver a push to every active endpoint for ``user_id``.

        Returns the number of endpoints the push was successfully dispatched to
        (0 when the user has no active device tokens). Never raises for a
        per-endpoint failure — a stale token must not abort the batch job.
        """
        tokens = await self.db.get_user_device_tokens(user_id)
        endpoints: List[str] = [
            str(t["endpoint_arn"])
            for t in tokens
            if t.get("endpoint_arn") and t.get("is_active", True)
        ]
        if not endpoints:
            logger.info(f"No active device endpoints for user {user_id}; skipping push")
            return 0

        delivered = 0
        for arn in endpoints:
            try:
                msg_id = await self.sns.publish_to_endpoint_async(
                    arn, title, body, data=data
                )
                if msg_id:
                    delivered += 1
            except Exception as e:  # noqa: BLE001 - one bad endpoint must not abort
                logger.warning(f"Push publish failed for user {user_id} ({arn}): {e}")

        logger.info(
            f"Dispatched push to {delivered}/{len(endpoints)} endpoints "
            f"for user {user_id}"
        )
        return delivered
