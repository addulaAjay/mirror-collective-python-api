"""Domain model for ``mc_user_personalization`` (spec §3.4).

Per-user prefs / flags / recent_use / per-event helpfulness records. The
shape allows decay-aware scoring (spec §9.2 note) by storing helpfulness
events with timestamps rather than running totals only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# Cap on how many helpfulness events we retain per practice. Older events
# are dropped on next write. 50 is plenty for 21-day half-life decay scoring.
HELPFULNESS_EVENT_CAP_PER_PRACTICE = 50


@dataclass
class UserFlags:
    no_breathwork: bool = False
    reduced_motion: bool = False
    private_mode: bool = False


@dataclass
class HelpfulnessEvent:
    """One helpfulness vote. Used by the personalization scorer (spec §9.2)."""

    ts: str
    helpful: bool


@dataclass
class RecentUseEntry:
    """Per-practice recency row used for the recent_use_penalty term."""

    last_used_at: str
    count_30d: int = 1


@dataclass
class UserPersonalization:
    """One row per user; PK=user_id."""

    user_id: str = ""
    flags: UserFlags = field(default_factory=UserFlags)
    disallow_types: List[str] = field(default_factory=list)
    practice_helpfulness: Dict[str, List[HelpfulnessEvent]] = field(
        default_factory=dict
    )
    recent_use: Dict[str, RecentUseEntry] = field(default_factory=dict)
    time_of_day_history: Dict[str, int] = field(default_factory=dict)
    updated_at: str = field(default_factory=_utcnow_iso)

    def to_dynamodb_item(self) -> Dict[str, Any]:
        # asdict recursively expands nested dataclasses into plain dicts.
        item = asdict(self)
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "UserPersonalization":
        flags_in = item.get("flags") or {}
        helpfulness_in: Dict[str, List[Dict[str, Any]]] = (
            item.get("practice_helpfulness") or {}
        )
        recent_use_in: Dict[str, Dict[str, Any]] = item.get("recent_use") or {}

        flags = UserFlags(
            no_breathwork=bool(flags_in.get("no_breathwork", False)),
            reduced_motion=bool(flags_in.get("reduced_motion", False)),
            private_mode=bool(flags_in.get("private_mode", False)),
        )
        helpfulness = {
            pid: [
                HelpfulnessEvent(ts=str(e["ts"]), helpful=bool(e["helpful"]))
                for e in events
            ]
            for pid, events in helpfulness_in.items()
        }
        recent_use = {
            pid: RecentUseEntry(
                last_used_at=str(entry.get("last_used_at", "")),
                count_30d=int(entry.get("count_30d", 0)),
            )
            for pid, entry in recent_use_in.items()
        }
        return cls(
            user_id=str(item.get("user_id", "")),
            flags=flags,
            disallow_types=list(item.get("disallow_types") or []),
            practice_helpfulness=helpfulness,
            recent_use=recent_use,
            time_of_day_history={
                k: int(v) for k, v in (item.get("time_of_day_history") or {}).items()
            },
            updated_at=str(item.get("updated_at") or _utcnow_iso()),
        )

    def append_helpfulness(
        self, practice_id: str, helpful: bool, ts: Optional[str] = None
    ) -> None:
        """Record one helpfulness vote, capped at HELPFULNESS_EVENT_CAP_PER_PRACTICE."""
        timestamp = ts or _utcnow_iso()
        events = self.practice_helpfulness.setdefault(practice_id, [])
        events.append(HelpfulnessEvent(ts=timestamp, helpful=helpful))
        if len(events) > HELPFULNESS_EVENT_CAP_PER_PRACTICE:
            del events[: len(events) - HELPFULNESS_EVENT_CAP_PER_PRACTICE]
        self.updated_at = timestamp

    def record_use(self, practice_id: str, ts: Optional[str] = None) -> None:
        """Update recent_use[practice_id]."""
        timestamp = ts or _utcnow_iso()
        existing = self.recent_use.get(practice_id)
        if existing is None:
            self.recent_use[practice_id] = RecentUseEntry(
                last_used_at=timestamp, count_30d=1
            )
        else:
            existing.last_used_at = timestamp
            existing.count_30d += 1
        self.updated_at = timestamp

    def increment_bucket(self, bucket_name: str) -> None:
        """+1 for a time-of-day bucket (used by personalization scorer)."""
        self.time_of_day_history[bucket_name] = (
            self.time_of_day_history.get(bucket_name, 0) + 1
        )
        self.updated_at = _utcnow_iso()
