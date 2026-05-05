"""DynamoDB repo for ``mc_reflection_sessions`` (spec §3.1).

Public methods:
  * ``put(session)`` — write or overwrite a session row
  * ``get(session_id)`` — fetch by primary key
  * ``get_latest_for_user(user_id)`` — most recent session via GSI
  * ``update_room_skin(session_id, room_skin_override)`` — for PUT /me/reflection/room
  * ``update_motif_and_quiz(...)`` — for "different answers within active session" overwrite
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

from ..core.exceptions import InternalServerError
from ..models.reflection_session import ReflectionSession
from ._base import _RepoBase
from ._serializers import from_ddb, to_ddb

logger = logging.getLogger(__name__)

GSI_USER_CREATED = "user_id-created_at-index"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ReflectionSessionRepo(_RepoBase):
    """DAO for the reflection_sessions table."""

    def __init__(self, session: Optional[Any] = None):
        super().__init__(session=session)
        self.table_name = os.getenv(
            "DYNAMODB_REFLECTION_SESSIONS_TABLE",
            "mc_reflection_sessions-development",
        )

    async def put(self, item: ReflectionSession) -> ReflectionSession:
        """Insert or overwrite a session."""
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                payload = to_ddb(item.to_dynamodb_item())
                await table.put_item(Item=payload)
            return item
        except ClientError as exc:
            logger.error(f"DDB put error on {self.table_name}: {exc}")
            raise InternalServerError(
                f"Failed to write reflection session: {exc}"
            ) from exc

    async def get(self, session_id: str) -> Optional[ReflectionSession]:
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                response = await table.get_item(Key={"session_id": session_id})
            item = response.get("Item")
            if item is None:
                return None
            return ReflectionSession.from_dynamodb_item(from_ddb(item))
        except ClientError as exc:
            logger.error(f"DDB get error on {self.table_name}: {exc}")
            raise InternalServerError(
                f"Failed to read reflection session: {exc}"
            ) from exc

    async def get_latest_for_user(self, user_id: str) -> Optional[ReflectionSession]:
        """Most-recent session via GSI (sort by created_at desc, limit 1)."""
        try:
            from boto3.dynamodb.conditions import Key

            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                response = await table.query(
                    IndexName=GSI_USER_CREATED,
                    KeyConditionExpression=Key("user_id").eq(user_id),
                    ScanIndexForward=False,  # newest first
                    Limit=1,
                )
            items: List[Dict[str, Any]] = response.get("Items") or []
            if not items:
                return None
            return ReflectionSession.from_dynamodb_item(from_ddb(items[0]))
        except ClientError as exc:
            logger.error(f"DDB query error on {self.table_name}: {exc}")
            raise InternalServerError(f"Failed to query latest session: {exc}") from exc

    async def update_room_skin(
        self, session_id: str, room_skin_override: str
    ) -> Optional[ReflectionSession]:
        """Set/replace ``room_skin_override`` on an existing row."""
        existing = await self.get(session_id)
        if existing is None:
            return None
        existing.room_skin_override = room_skin_override
        existing.updated_at = _utcnow_iso()
        return await self.put(existing)

    async def update_motif_and_quiz(
        self,
        session_id: str,
        motif_id: str,
        motif_name: str,
        room_skin: str,
        motif_payload: Dict[str, Any],
        quiz_answers: Dict[str, str],
        scores: Dict[str, int],
    ) -> Optional[ReflectionSession]:
        """Overwrite quiz/motif fields on an existing session.

        Used when the user retakes the quiz with different answers within the
        same active session (spec §6.1 + §8.3 reseeding rules).
        """
        existing = await self.get(session_id)
        if existing is None:
            return None
        existing.motif_id = motif_id
        existing.motif_name = motif_name
        existing.room_skin = room_skin
        existing.motif_payload = motif_payload
        existing.quiz_answers = quiz_answers
        existing.scores = scores
        existing.room_skin_override = None  # new motif clears any override
        existing.updated_at = _utcnow_iso()
        return await self.put(existing)
