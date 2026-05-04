"""Domain model for ``mc_reflection_sessions`` (spec §3.1).

Session lifetime is until next midnight in the user's IANA timezone (or
``REFLECTION_DEFAULT_USER_TZ`` if unknown). The ``expires_at`` field is set
at creation; mid-day midnight crossings do **not** slide it forward.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4


def _generate_id() -> str:
    return str(uuid4())


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ReflectionSession:
    """One session row. PK=session_id; GSI=user_id-created_at-index."""

    session_id: str = field(default_factory=_generate_id)
    user_id: str = ""

    # Motif (resolved at quiz submit).
    motif_id: str = ""
    motif_name: str = ""
    room_skin: str = ""
    motif_payload: Dict[str, Any] = field(default_factory=dict)

    # Quiz inputs + scores.
    quiz_answers: Dict[str, str] = field(default_factory=dict)
    scores: Dict[str, int] = field(default_factory=dict)

    # Optional override (PUT /me/reflection/room with apply_to=session).
    room_skin_override: Optional[str] = None

    # Session lifetime (spec §3.1 + §6.1).
    user_tz: str = "America/New_York"
    expires_at: str = ""  # ISO of next-midnight in user_tz

    # Bookkeeping.
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)
    ttl: int = 0  # Epoch seconds; 30 days from created_at (storage cleanup only)

    def effective_room_skin(self) -> str:
        """Override wins over default. Used by snapshot endpoint."""
        return self.room_skin_override or self.room_skin

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DDB-shaped dict (floats → Decimal handled by repo)."""
        item = asdict(self)
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "ReflectionSession":
        """Construct from a DDB read result. Caller must run from_ddb first."""
        # Tolerate forward-compatible fields by filtering to known ones.
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in item.items() if k in known}
        return cls(**filtered)
