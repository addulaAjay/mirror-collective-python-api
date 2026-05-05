"""Domain model for ``mc_echo_loop_state`` (spec §3.2).

Active loop state is the source data behind ``GET /echo/snapshot``. PK=user_id,
SK=loop_id (one of the 6 V1 loop families). Seeded from quiz answers and
mutated by practice completions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class EchoLoopState:
    """One (user_id, loop_id) row in the active loop-state table."""

    user_id: str = ""
    loop_id: str = (
        ""  # one of: pressure, overwhelm, grief, self_silencing, agency, transition
    )
    tone_state: str = "rising"  # rising | steady | softening
    intensity_score: float = 0.0
    intensity_label: str = "Low"  # High | Medium | Low
    last_seen: str = field(default_factory=_utcnow_iso)
    recently_changed: bool = False
    narrative_stage: Optional[str] = None
    updated_at: str = field(default_factory=_utcnow_iso)

    def to_dynamodb_item(self) -> Dict[str, Any]:
        item = asdict(self)
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "EchoLoopState":
        known = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in item.items() if k in known}
        return cls(**filtered)
