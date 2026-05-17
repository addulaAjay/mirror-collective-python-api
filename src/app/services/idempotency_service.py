"""Idempotency cache for write endpoints.

Lets a client safely retry a POST without producing duplicate writes:

1. Client sends an ``Idempotency-Key`` header (a UUID per logical request).
2. Server checks the cache. On hit: returns the stored response verbatim.
3. On miss: serves the request normally, then stores ``(status, body)``
   under the key with a 24 h TTL.

The cache key is namespaced ``"<user_id>|<route>|<client_key>"`` so that
keys are scoped per-user + per-route — a client cannot pollute another
user's namespace, and the same UUID can be reused across distinct routes
without collision.

Storage is the ``IdempotencyTable`` defined in ``serverless.yml`` (PK is
``key_namespace``; TTL is ``expires_at`` epoch seconds, deleted within
~48 h of the timestamp passing per the DynamoDB TTL contract).

Design notes
------------
- Writes are conditional on the key NOT already existing
  (``attribute_not_exists(key_namespace)``). If two concurrent requests
  race, the second one's write fails harmlessly and the second client
  gets a fresh response (acceptable — they raced and the server already
  handled both). The next retry from either client sees the cached
  result.
- ``response_body`` is stored as a JSON-encoded string rather than a
  native DynamoDB map so we don't have to round-trip type coercions
  (Decimal ↔ float, etc.) — the API serialization is the source of
  truth, and replaying it is byte-equivalent.
"""

import json
import logging
import os
import time
from contextlib import AsyncExitStack
from typing import Any, Dict, Optional

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# 24 hours. Long enough to cover all realistic client retry windows
# (timeout + manual user retry) without keeping the table large. Tied
# to the comment on DYNAMODB_IDEMPOTENCY_TABLE in serverless.yml; if
# you tune this, update both.
IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60


def build_namespace(user_id: str, route: str, client_key: str) -> str:
    """Build the composite cache key. Public for callers that want to
    inspect or pre-compute it (e.g. tests).
    """
    return f"{user_id}|{route}|{client_key}"


class IdempotencyService:
    """Thin DDB wrapper for caching idempotent route responses.

    Mirrors the long-lived-client pattern used by EchoService (aioboto3
    resource cached in an AsyncExitStack for the lifetime of the Lambda
    container).
    """

    def __init__(self) -> None:
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.table_name = os.getenv(
            "DYNAMODB_IDEMPOTENCY_TABLE",
            "mirror-collective-python-api-idempotency-development",
        )
        self.endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL")

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

    async def get_cached(
        self,
        user_id: str,
        route: str,
        client_key: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the previously-stored response for this key, or None.

        Returns a dict ``{status_code: int, body: dict}`` ready for the
        route handler to relay. ``None`` means cache miss (or expired
        before DDB's TTL sweep ran — handled the same way).
        """
        ns = build_namespace(user_id, route, client_key)
        try:
            ddb = await self._get_resource()
            table = await ddb.Table(self.table_name)
            response = await table.get_item(Key={"key_namespace": ns})
        except ClientError as e:
            # Don't fail the user's request because the cache is sick.
            # Idempotency is opportunistic — the route handler will run
            # again as if there were no cache.
            logger.error(f"Idempotency get_item failed for {ns}: {e}")
            return None

        item = response.get("Item")
        if not item:
            return None

        # Honor expires_at even if DDB hasn't pruned yet — the TTL sweep
        # runs every ~48 h, not on access. A late retry against an
        # already-stale entry shouldn't replay.
        expires_at = int(item.get("expires_at") or 0)
        if expires_at and expires_at <= int(time.time()):
            return None

        raw_body = item.get("response_body")
        try:
            body = json.loads(raw_body) if raw_body else {}
        except (TypeError, ValueError):
            # Corrupt cache row — treat as miss so the route can serve
            # fresh. The next write will overwrite this row.
            logger.warning(f"Idempotency row {ns} has malformed body; treating as miss")
            return None

        return {
            "status_code": int(item.get("status_code") or 200),
            "body": body,
        }

    async def cache(
        self,
        user_id: str,
        route: str,
        client_key: str,
        status_code: int,
        body: Dict[str, Any],
    ) -> None:
        """Store the response under the key. No-op on race (concurrent
        write of the same key).
        """
        ns = build_namespace(user_id, route, client_key)
        now = int(time.time())
        item = {
            "key_namespace": ns,
            "user_id": user_id,
            "route": route,
            "client_key": client_key,
            "status_code": int(status_code),
            "response_body": json.dumps(body, default=str),
            "created_at": now,
            "expires_at": now + IDEMPOTENCY_TTL_SECONDS,
        }
        try:
            ddb = await self._get_resource()
            table = await ddb.Table(self.table_name)
            await table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(key_namespace)",
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                # Two requests with the same key landed in parallel.
                # The first one's write wins; the second is harmless.
                logger.info(f"Idempotency race resolved for {ns}")
                return
            logger.error(f"Idempotency put_item failed for {ns}: {e}")
            # Same opportunistic-failure posture as get_cached.


_SINGLETON: Optional[IdempotencyService] = None


def get_idempotency_service() -> IdempotencyService:
    """Module-level singleton — mirror EchoService's lazy pattern."""
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = IdempotencyService()
    return _SINGLETON
