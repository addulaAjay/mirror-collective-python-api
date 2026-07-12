"""DynamoDB repo for the life-anchors table (MirrorGPT Memory — Phase 2).

Public methods:
  * ``query_by_user(user_id)`` — all anchors for a user
  * ``get(user_id, anchor_id)`` — single anchor
  * ``upsert(anchor)`` — create or overwrite one anchor
  * ``delete(user_id, anchor_id)`` — hard-delete one anchor
  * ``list_active_for_user(user_id)`` — active anchors scoped to MirrorGPT
    (used by the Phase 2C preflight injection)
"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from botocore.exceptions import ClientError

from ..core.exceptions import InternalServerError
from ..models.life_anchor import LifeAnchor
from ._base import _RepoBase
from ._serializers import from_ddb, to_ddb

logger = logging.getLogger(__name__)


class LifeAnchorRepo(_RepoBase):
    """DAO for the life_anchors table."""

    def __init__(self, session: Optional[Any] = None):
        super().__init__(session=session)
        self.table_name = os.getenv(
            "DYNAMODB_LIFE_ANCHORS_TABLE",
            "mc_life_anchors-development",
        )

    async def query_by_user(self, user_id: str) -> List[LifeAnchor]:
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
            return [LifeAnchor.from_dynamodb_item(from_ddb(i)) for i in items]
        except ClientError as exc:
            logger.error(f"DDB query error on {self.table_name}: {exc}")
            raise InternalServerError(f"Failed to query life anchors: {exc}") from exc

    async def get(self, user_id: str, anchor_id: str) -> Optional[LifeAnchor]:
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                response = await table.get_item(
                    Key={"user_id": user_id, "anchor_id": anchor_id}
                )
            item = response.get("Item")
            if item is None:
                return None
            return LifeAnchor.from_dynamodb_item(from_ddb(item))
        except ClientError as exc:
            logger.error(f"DDB get error on {self.table_name}: {exc}")
            raise InternalServerError(f"Failed to read life anchor: {exc}") from exc

    async def upsert(self, anchor: LifeAnchor) -> LifeAnchor:
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                payload = to_ddb(anchor.to_dynamodb_item())
                await table.put_item(Item=payload)
            return anchor
        except ClientError as exc:
            logger.error(f"DDB put error on {self.table_name}: {exc}")
            raise InternalServerError(f"Failed to upsert life anchor: {exc}") from exc

    async def delete(self, user_id: str, anchor_id: str) -> None:
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                await table.delete_item(
                    Key={"user_id": user_id, "anchor_id": anchor_id}
                )
        except ClientError as exc:
            logger.error(f"DDB delete error on {self.table_name}: {exc}")
            raise InternalServerError(f"Failed to delete life anchor: {exc}") from exc

    async def list_active_for_user(self, user_id: str) -> List[LifeAnchor]:
        """Active anchors the user has scoped to MirrorGPT.

        Filters in-app rather than via the status GSI — a user has at most a
        handful of anchors, so a single query + filter is cheaper than a GSI
        round-trip. Used by the Phase 2C preflight injection.
        """
        anchors = await self.query_by_user(user_id)
        return [a for a in anchors if a.status == "active" and a.scopes.mirrorgpt]
