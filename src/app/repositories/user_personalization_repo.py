"""DynamoDB repo for ``mc_user_personalization`` (spec §3.4).

Public methods:
  * ``get_or_default(user_id)`` — never returns None; new users get default flags
  * ``upsert(prefs)`` — write the full row
  * ``record_completion(user_id, practice_id, time_of_day_bucket)`` — one combined helper
  * ``record_helpfulness(user_id, practice_id, helpful, ts)`` — append vote event
  * ``set_flags(user_id, **flag_overrides)`` — partial flag update
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from botocore.exceptions import ClientError

from ..core.exceptions import InternalServerError
from ..models.user_personalization import UserPersonalization
from ._base import _RepoBase
from ._serializers import from_ddb, to_ddb

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class UserPersonalizationRepo(_RepoBase):
    """DAO for the user_personalization table."""

    def __init__(self, session: Optional[Any] = None):
        super().__init__(session=session)
        self.table_name = os.getenv(
            "DYNAMODB_USER_PERSONALIZATION_TABLE",
            "mc_user_personalization-development",
        )

    async def _get_raw(self, user_id: str) -> Optional[UserPersonalization]:
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                response = await table.get_item(Key={"user_id": user_id})
            item = response.get("Item")
            if item is None:
                return None
            return UserPersonalization.from_dynamodb_item(from_ddb(item))
        except ClientError as exc:
            logger.error(f"DDB get error on {self.table_name}: {exc}")
            raise InternalServerError(
                f"Failed to read user personalization: {exc}"
            ) from exc

    async def get_or_default(self, user_id: str) -> UserPersonalization:
        existing = await self._get_raw(user_id)
        if existing is not None:
            return existing
        return UserPersonalization(user_id=user_id)

    async def upsert(self, prefs: UserPersonalization) -> UserPersonalization:
        prefs.updated_at = _utcnow_iso()
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as ddb:
                table = await ddb.Table(self.table_name)
                payload = to_ddb(prefs.to_dynamodb_item())
                await table.put_item(Item=payload)
            return prefs
        except ClientError as exc:
            logger.error(f"DDB put error on {self.table_name}: {exc}")
            raise InternalServerError(
                f"Failed to upsert user personalization: {exc}"
            ) from exc

    async def record_completion(
        self, user_id: str, practice_id: str, time_of_day_bucket: str
    ) -> UserPersonalization:
        """Combine the side-effects of one practice completion: bump bucket, refresh recent_use."""
        prefs = await self.get_or_default(user_id)
        prefs.record_use(practice_id)
        prefs.increment_bucket(time_of_day_bucket)
        return await self.upsert(prefs)

    async def record_helpfulness(
        self,
        user_id: str,
        practice_id: str,
        helpful: bool,
        ts: Optional[str] = None,
    ) -> UserPersonalization:
        prefs = await self.get_or_default(user_id)
        prefs.append_helpfulness(practice_id, helpful, ts)
        return await self.upsert(prefs)

    async def set_flags(
        self,
        user_id: str,
        no_breathwork: Optional[bool] = None,
        reduced_motion: Optional[bool] = None,
        private_mode: Optional[bool] = None,
    ) -> UserPersonalization:
        prefs = await self.get_or_default(user_id)
        if no_breathwork is not None:
            prefs.flags.no_breathwork = no_breathwork
        if reduced_motion is not None:
            prefs.flags.reduced_motion = reduced_motion
        if private_mode is not None:
            prefs.flags.private_mode = private_mode
        return await self.upsert(prefs)
