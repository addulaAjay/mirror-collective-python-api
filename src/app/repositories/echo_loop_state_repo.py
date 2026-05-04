"""DynamoDB repo for ``mc_echo_loop_state`` (spec §3.2).

Public methods:
  * ``query_by_user(user_id)`` — all loop rows for a user
  * ``get(user_id, loop_id)`` — single loop row
  * ``upsert(state)`` — write or overwrite one row
  * ``upsert_many(states)`` — bulk write (used by quiz seeder, spec §8.3)
  * ``delete_for_user(user_id)`` — wipe all rows for a user (test helper)
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from botocore.exceptions import ClientError

from ..core.exceptions import InternalServerError
from ..models.echo_loop_state import EchoLoopState
from ._base import _RepoBase
from ._serializers import from_ddb, to_ddb

logger = logging.getLogger(__name__)


class EchoLoopStateRepo(_RepoBase):
    """DAO for the echo_loop_state table."""

    def __init__(self, session: Optional[Any] = None):
        super().__init__(session=session)
        self.table_name = os.getenv(
            "DYNAMODB_ECHO_LOOP_STATE_TABLE",
            "mc_echo_loop_state-development",
        )

    async def query_by_user(self, user_id: str) -> List[EchoLoopState]:
        try:
            from boto3.dynamodb.conditions import Key

            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                response = await table.query(
                    KeyConditionExpression=Key("user_id").eq(user_id)
                )
            items = response.get("Items") or []
            return [EchoLoopState.from_dynamodb_item(from_ddb(i)) for i in items]
        except ClientError as exc:
            logger.error(f"DDB query error on {self.table_name}: {exc}")
            raise InternalServerError(
                f"Failed to query echo loop state: {exc}"
            ) from exc

    async def get(self, user_id: str, loop_id: str) -> Optional[EchoLoopState]:
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                response = await table.get_item(
                    Key={"user_id": user_id, "loop_id": loop_id}
                )
            item = response.get("Item")
            if item is None:
                return None
            return EchoLoopState.from_dynamodb_item(from_ddb(item))
        except ClientError as exc:
            logger.error(f"DDB get error on {self.table_name}: {exc}")
            raise InternalServerError(f"Failed to read echo loop state: {exc}") from exc

    async def upsert(self, state: EchoLoopState) -> EchoLoopState:
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                payload = to_ddb(state.to_dynamodb_item())
                await table.put_item(Item=payload)
            return state
        except ClientError as exc:
            logger.error(f"DDB put error on {self.table_name}: {exc}")
            raise InternalServerError(
                f"Failed to upsert echo loop state: {exc}"
            ) from exc

    async def upsert_many(self, states: List[EchoLoopState]) -> List[EchoLoopState]:
        """Bulk upsert. V1 uses sequential put_item (max 6 rows per quiz)."""
        results: List[EchoLoopState] = []
        for s in states:
            results.append(await self.upsert(s))
        return results

    async def delete_for_user(self, user_id: str) -> int:
        """Wipe all loop rows for a user. Returns count deleted."""
        rows = await self.query_by_user(user_id)
        if not rows:
            return 0
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                for r in rows:
                    await table.delete_item(
                        Key={"user_id": r.user_id, "loop_id": r.loop_id}
                    )
            return len(rows)
        except ClientError as exc:
            logger.error(f"DDB delete error on {self.table_name}: {exc}")
            raise InternalServerError(
                f"Failed to delete echo loop state: {exc}"
            ) from exc
