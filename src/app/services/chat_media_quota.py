"""Per-user daily cap on MirrorGPT vision turns.

Why this exists
---------------
There is no per-user rate limiting anywhere else in this service. The old
in-memory limiter was deliberately deleted (see ``handler.py``) because it
only counted requests within a single Lambda container, and what replaced it
is a **global** API Gateway throttle — 1000 rps across all users
(``serverless.yml`` ``DefaultRouteSettings``).

A global throttle does nothing about the failure mode that matters here: one
account sending photo after photo and running up OpenAI vision cost. Vision
is the one path in this codebase where a single user can spend real money per
request, so it gets the first per-user cap.

Where the counter lives
-----------------------
On the existing ``ActivityTable`` row, keyed ``{user_id, activity_date}`` —
the same row that already carries per-user-per-day counters like
``chat_messages`` (see ``DynamoDBService.record_activity``). That means:

* no new table, no ``serverless.yml`` change, no new IAM statement (the
  Lambda role already has ``UpdateItem`` on this table);
* the row is naturally scoped to one day, so "reset at midnight UTC" needs no
  cleanup job and no TTL — tomorrow is simply a different row.

Why one conditional write
-------------------------
The consume is a SINGLE ``update_item`` that increments and enforces the limit
in the same atomic operation. A read-then-write ("is the count below the
limit? then add one") would let two concurrent requests both read a count
under the limit and both proceed — which is exactly the burst this is meant
to stop.

Failure policy
--------------
Fails **open**. A DynamoDB problem means the turn proceeds, matching the
existing ``QuotaEnforcementMiddleware`` ("fail open to avoid blocking
legitimate users"). Taking chat down to protect a cost control is the wrong
trade, and the global throttle still applies underneath. Every fail-open is
logged at WARNING so the gap is visible rather than silent.
"""

from __future__ import annotations

import logging
import os
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from ..core.exceptions import RateLimitExceededError

logger = logging.getLogger(__name__)

# ── Enforcement switch ───────────────────────────────────────────────────────
#
# OFF by default, by product decision: the caps are not enforced while chat
# media is being tracked at branch level, so early usage isn't shaped by a
# limit nobody has calibrated yet.
#
# The accounting still runs — every chargeable turn is counted on the daily
# activity row whether or not the limit is enforced. That is deliberate: it
# means the counters (and the chat_media telemetry alongside them) produce the
# real usage distribution needed to pick a sensible number, and turning
# enforcement on later is one env var rather than a code change.
#
# COST EXPOSURE WHILE OFF: there is no other per-user limit anywhere in this
# service — the only backstop is a GLOBAL API Gateway throttle (1000 rps),
# which does nothing about one account running up OpenAI spend. Vision is
# ~$0.0004/turn and transcription ~$0.006/minute, so a single determined
# account could spend real money. Flip this on before any public launch.
MEDIA_CAPS_ENABLED = (
    os.getenv("MIRRORGPT_MEDIA_CAPS_ENABLED", "false").lower() == "true"
)

# Used only when MEDIA_CAPS_ENABLED is true. 25/day against a usage model of
# ~2 photos/user/day (plan §7) — a runaway-cost backstop, not a rationing
# device. Tune via env without a code change.
DAILY_VISION_LIMIT = int(os.getenv("MIRRORGPT_DAILY_VISION_LIMIT", "25"))

# Tighter than vision on purpose. Whisper bills per minute and works out at
# roughly 10x the per-event cost of a low-detail image (plan §7), so the same
# numeric cap would not bound spend comparably. 10/day still covers the usage
# model of ~3 voice notes/user/day with headroom.
DAILY_TRANSCRIPTION_LIMIT = int(os.getenv("MIRRORGPT_DAILY_TRANSCRIPTION_LIMIT", "10"))

# Attributes on the daily activity row. Counted separately so it stays visible
# which feature is driving spend — one shared counter would let photo use
# exhaust the voice allowance and vice versa.
_VISION_COUNTER_ATTR = "vision_turns"
_TRANSCRIPTION_COUNTER_ATTR = "transcription_turns"


class ChatMediaQuotaService:
    """Daily vision-turn accounting on the user's activity row."""

    def __init__(self) -> None:
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.table_name = os.getenv("DYNAMODB_ACTIVITY_TABLE", "user_activity")
        self.endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL")

        # Long-lived client cached for the Lambda container's lifetime, the
        # same pattern EchoService and IdempotencyService use.
        self._session = aioboto3.Session()
        self._exit_stack: Optional[AsyncExitStack] = None
        self._dynamodb_resource: Any = None
        self._boto_config = Config(
            max_pool_connections=25,
            retries={"max_attempts": 3, "mode": "adaptive"},
        )

    def _ddb_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "region_name": self.region,
            "config": self._boto_config,
        }
        if self.endpoint_url:
            kwargs.update(
                {
                    "endpoint_url": self.endpoint_url,
                    "aws_access_key_id": "dummy",
                    "aws_secret_access_key": "dummy",
                }
            )
        return kwargs

    async def _get_resource(self) -> Any:
        if self._dynamodb_resource is not None:
            return self._dynamodb_resource
        if self._exit_stack is None:
            self._exit_stack = AsyncExitStack()
            await self._exit_stack.__aenter__()
        cm = self._session.resource("dynamodb", **self._ddb_kwargs())
        self._dynamodb_resource = await self._exit_stack.enter_async_context(cm)
        return self._dynamodb_resource

    async def _consume(
        self,
        user_id: str,
        counter_attr: str,
        limit: int,
        label: str,
        over_limit_message: str,
    ) -> None:
        """Count one chargeable turn against today's allowance.

        Raises:
            RateLimitExceededError: the user is at the daily cap (HTTP 429).

        Returns normally — permitting the turn — both when the user is under
        the cap and when the check could not be performed (see "Failure
        policy" in the module docstring).
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        try:
            dynamodb = await self._get_resource()
            table = await dynamodb.Table(self.table_name)

            # Increment and enforce in one atomic operation.
            #
            # `attribute_not_exists` covers the first chargeable turn of the
            # day: the row may already exist (record_activity writes
            # chat_messages to it) but without this attribute, and a bare
            # `< :limit` comparison against a missing attribute fails the
            # condition — which would reject the user's very first photo or
            # recording each day.
            await table.update_item(
                Key={"user_id": user_id, "activity_date": today},
                UpdateExpression=f"ADD {counter_attr} :inc",
                # The condition is what enforces the cap. With enforcement
                # off we still ADD — so usage is measured — but drop the
                # condition so the write always succeeds.
                **(
                    {
                        "ConditionExpression": (
                            f"attribute_not_exists({counter_attr}) "
                            f"OR {counter_attr} < :limit"
                        ),
                        "ExpressionAttributeValues": {
                            ":inc": 1,
                            ":limit": limit,
                        },
                    }
                    if MEDIA_CAPS_ENABLED
                    else {"ExpressionAttributeValues": {":inc": 1}}
                ),
            )

        except ClientError as err:
            code = err.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                # Genuinely over the limit. The message is user-facing — it
                # reaches the chat UI — so it reads as guidance, not as an
                # internal limit name.
                logger.info(
                    "%s daily cap reached for user_id=%s (limit=%d)",
                    label,
                    user_id,
                    limit,
                )
                raise RateLimitExceededError(
                    over_limit_message,
                    retry_after=_seconds_until_utc_midnight(),
                ) from err

            # Any other AWS error: fail open.
            logger.warning(
                "%s quota check failed open for user_id=%s: %s", label, user_id, err
            )

        except RateLimitExceededError:
            # Never let the broad handler below swallow our own 429 — that
            # would make the cap decorative.
            raise

        except Exception as err:
            logger.warning(
                "%s quota check failed open for user_id=%s: %s", label, user_id, err
            )

    async def consume_vision_turn(self, user_id: str) -> None:
        """Count one photo (vision) turn against today's allowance."""
        await self._consume(
            user_id=user_id,
            counter_attr=_VISION_COUNTER_ATTR,
            limit=DAILY_VISION_LIMIT,
            label="vision",
            over_limit_message=(
                "You've shared the most photos the Mirror can take in one "
                "day. Try again tomorrow."
            ),
        )

    async def consume_transcription_turn(self, user_id: str) -> None:
        """Count one voice transcription against today's allowance."""
        await self._consume(
            user_id=user_id,
            counter_attr=_TRANSCRIPTION_COUNTER_ATTR,
            limit=DAILY_TRANSCRIPTION_LIMIT,
            label="transcription",
            over_limit_message=(
                "You've sent the most voice notes the Mirror can hear in one "
                "day. Try again tomorrow, or type instead."
            ),
        )


def _seconds_until_utc_midnight() -> int:
    """Seconds until the counter resets, for the Retry-After hint."""
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


_quota_service: Optional[ChatMediaQuotaService] = None


def get_chat_media_quota_service() -> ChatMediaQuotaService:
    """Module-level singleton, so the aioboto3 client is reused per container."""
    global _quota_service
    if _quota_service is None:
        _quota_service = ChatMediaQuotaService()
    return _quota_service
