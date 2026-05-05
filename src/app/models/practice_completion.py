"""Domain model for ``mc_practice_completions`` (spec §3.3).

PK=user_id, SK=completion_id formatted as ``"<ts_iso>#<uuid>"`` so per-user
list scans return rows in completion order. ``user_hash`` lets audit logs
join without exposing user_id.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _generate_completion_id(completed_at_iso: str) -> str:
    return f"{completed_at_iso}#{uuid4()}"


def _hash_user(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]


@dataclass
class PracticeCompletion:
    """One practice completion row."""

    user_id: str = ""
    completion_id: str = ""  # set in __post_init__ if blank
    session_id: str = ""
    loop_id: str = ""
    tone_state: str = ""  # at time of action
    practice_id: str = ""
    rule_id: str = ""  # one of the rule IDs or "fallback"
    helpful: Optional[bool] = None  # null until user votes
    completed_at: str = field(default_factory=_utcnow_iso)
    user_hash: str = ""

    def __post_init__(self) -> None:
        if not self.completion_id:
            self.completion_id = _generate_completion_id(self.completed_at)
        if not self.user_hash and self.user_id:
            self.user_hash = _hash_user(self.user_id)

    def to_dynamodb_item(self) -> Dict[str, Any]:
        item = asdict(self)
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "PracticeCompletion":
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in item.items() if k in known}
        return cls(**filtered)
