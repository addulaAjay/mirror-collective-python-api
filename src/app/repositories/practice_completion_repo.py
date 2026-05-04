"""DynamoDB repo for ``mc_practice_completions`` (spec §3.3).

Public methods:
  * ``put(completion)`` — write a new completion row
  * ``list_by_user_since(user_id, since)`` — completions on/after ``since``
  * ``update_helpful(user_id, completion_id, helpful)`` — vote update
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, List, Optional

from botocore.exceptions import ClientError

from ..core.exceptions import InternalServerError
from ..models.practice_completion import PracticeCompletion
from ._base import _RepoBase
from ._serializers import from_ddb

logger = logging.getLogger(__name__)


def _to_iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class PracticeCompletionRepo(_RepoBase):
    """DAO for the practice_completions table."""

    def __init__(self, session: Optional[Any] = None):
        super().__init__(session=session)
        self.table_name = os.getenv(
            "DYNAMODB_PRACTICE_COMPLETIONS_TABLE",
            "mc_practice_completions-development",
        )

    async def put(self, item: PracticeCompletion) -> PracticeCompletion:
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                await table.put_item(Item=item.to_dynamodb_item())
            return item
        except ClientError as exc:
            logger.error(f"DDB put error on {self.table_name}: {exc}")
            raise InternalServerError(
                f"Failed to write practice completion: {exc}"
            ) from exc

    async def list_by_user_since(
        self, user_id: str, since: datetime
    ) -> List[PracticeCompletion]:
        """Return all completions for ``user_id`` with ``completed_at >= since``.

        Uses ``completion_id BETWEEN`` because ``completion_id`` is sortable as
        ``"<ts_iso>#<uuid>"`` — a string comparison against the lower-bound
        timestamp is order-correct.
        """
        try:
            from boto3.dynamodb.conditions import Key

            since_iso = _to_iso(since)
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                response = await table.query(
                    KeyConditionExpression=(
                        Key("user_id").eq(user_id) & Key("completion_id").gte(since_iso)
                    )
                )
            items = response.get("Items") or []
            return [PracticeCompletion.from_dynamodb_item(from_ddb(i)) for i in items]
        except ClientError as exc:
            logger.error(f"DDB query error on {self.table_name}: {exc}")
            raise InternalServerError(
                f"Failed to list practice completions: {exc}"
            ) from exc

    async def update_helpful(
        self, user_id: str, completion_id: str, helpful: bool
    ) -> Optional[PracticeCompletion]:
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                # Read-modify-write — V1 traffic is too low to need atomic update.
                response = await table.get_item(
                    Key={"user_id": user_id, "completion_id": completion_id}
                )
                item = response.get("Item")
                if item is None:
                    return None
                model = PracticeCompletion.from_dynamodb_item(from_ddb(item))
                model.helpful = helpful
                await table.put_item(Item=model.to_dynamodb_item())
                return model
        except ClientError as exc:
            logger.error(f"DDB update error on {self.table_name}: {exc}")
            raise InternalServerError(
                f"Failed to update practice completion: {exc}"
            ) from exc
