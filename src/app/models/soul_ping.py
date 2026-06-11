"""
Soul Ping models — proactive, conversation-aware notifications.

A Soul Ping is an MC-branded nudge generated from a user's recent mirror-chat
history + continuity summary and delivered as a push notification. v1 supports
three categories (emotional / progress / systemic) and is throttled to at most
one ping per user per hour by the dispatch job.

See docs/SOUL_PINGS_PRD.md (frontend repo) for the product framing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class SoulPingCategory(str, Enum):
    """The Soul Ping categories enabled in v1.

    `str` mixin so the value serializes directly to DynamoDB / JSON and can be
    compared to plain strings (e.g. a user's enabled-category list).
    """

    EMOTIONAL = "emotional"
    PROGRESS = "progress"
    SYSTEMIC = "systemic"

    @classmethod
    def from_value(cls, value: Optional[str]) -> Optional["SoulPingCategory"]:
        """Parse a string to a category, or None if it isn't a v1 category."""
        if not value:
            return None
        try:
            return cls(value.strip().lower())
        except ValueError:
            return None


# The categories shipped in v1. Used as the default enabled-set when a user has
# no explicit preference, and to validate LLM-chosen categories.
V1_CATEGORIES: List[SoulPingCategory] = [
    SoulPingCategory.EMOTIONAL,
    SoulPingCategory.PROGRESS,
    SoulPingCategory.SYSTEMIC,
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class SoulPing:
    """A single generated + sent Soul Ping.

    Stored in the soul-pings table (PK user_id, SK sent_at) so the dispatch job
    can read the latest row to enforce the one-per-hour throttle, and so a
    future in-app feed has history.
    """

    user_id: str
    category: SoulPingCategory
    title: str
    body: str

    ping_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    deep_link: str = "SoulPing"
    source: str = "scheduled"  # "scheduled" | "manual"
    sent_at: str = field(default_factory=_now_iso)
    read_at: Optional[str] = None
    # Convenience: which conversation seeded this ping (for debugging / future feed).
    conversation_id: Optional[str] = None

    def to_dynamodb_item(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "sent_at": self.sent_at,
            "ping_id": self.ping_id,
            "category": self.category.value,
            "title": self.title,
            "body": self.body,
            "deep_link": self.deep_link,
            "source": self.source,
            **({"read_at": self.read_at} if self.read_at else {}),
            **(
                {"conversation_id": self.conversation_id}
                if self.conversation_id
                else {}
            ),
        }

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "SoulPing":
        category = SoulPingCategory.from_value(item.get("category")) or (
            SoulPingCategory.EMOTIONAL
        )
        return cls(
            user_id=item["user_id"],
            sent_at=item.get("sent_at", _now_iso()),
            ping_id=item.get("ping_id", str(uuid.uuid4())),
            category=category,
            title=item.get("title", ""),
            body=item.get("body", ""),
            deep_link=item.get("deep_link", "SoulPing"),
            source=item.get("source", "scheduled"),
            read_at=item.get("read_at"),
            conversation_id=item.get("conversation_id"),
        )

    def push_data(self) -> Dict[str, str]:
        """The `data` payload carried by the push (string values only — FCM/APNs
        require string maps). The app routes on `type` + `deep_link` and uses
        `ping_id` to mark-read / dedupe."""
        return {
            "type": "soul_ping",
            "ping_id": self.ping_id,
            "category": self.category.value,
            "deep_link": self.deep_link,
            "deep_link_params": f'{{"pingId":"{self.ping_id}"}}',
            "sent_at": self.sent_at,
        }
