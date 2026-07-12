"""Domain model for the Life Anchors table (MirrorGPT Memory — Phase 2).

A Life Anchor is a user-declared durable fact/event that permanently changes
the meaning of future reflections — a death, a birth, a divorce, a diagnosis,
sobriety, a traumatic anniversary, a major transition, or anything the user
explicitly marks as "remember this". Stored as a permissioned, user-scoped
entity: PK=user_id, SK=anchor_id. Never written without explicit user
confirmation. See docs/MIRRORGPT_MEMORY_PLAN.md Phase 2.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# Allowed values — validated at the API boundary (Pydantic Literals); stored
# as plain strings so the persistence layer stays schema-light.
ANCHOR_TYPES = (
    "loss",
    "birth",
    "divorce",
    "diagnosis",
    "sobriety",
    "anniversary",
    "transition",
    "custom",
)
EMOTIONAL_WEIGHTS = ("sacred", "high", "medium")
REFLECTION_USES = ("always_consider", "when_relevant", "never")
# "pending" is an internal staging status for the in-chat confirm flow (2D) —
# not user-settable and never injected (list_active_for_user requires "active").
STATUSES = ("active", "paused", "pending")


@dataclass
class AnchorScopes:
    """Where an anchor may be used. Defaults to MirrorGPT reflections only."""

    mirrorgpt: bool = True
    echo_map: bool = False
    echo_vault: bool = False
    legacy_capsule: bool = False


@dataclass
class LifeAnchor:
    """One (user_id, anchor_id) row in the life-anchors table."""

    user_id: str = ""
    anchor_id: str = ""
    anchor_type: str = "custom"
    title: str = ""
    description: str = ""
    relationship: Optional[str] = None
    date: Optional[str] = None
    emotional_weight: str = "medium"
    reflection_use: str = "when_relevant"
    status: str = "active"
    scopes: AnchorScopes = field(default_factory=AnchorScopes)
    tone_guidance: List[str] = field(default_factory=list)
    created_from: str = "mirrorgpt"
    user_confirmed: bool = True
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)

    def __post_init__(self) -> None:
        if not self.anchor_id:
            self.anchor_id = str(uuid4())

    def touch(self) -> None:
        """Bump updated_at to now."""
        self.updated_at = _utcnow_iso()

    def to_dynamodb_item(self) -> Dict[str, Any]:
        # asdict recursively expands AnchorScopes into a plain dict.
        item = asdict(self)
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "LifeAnchor":
        scopes_in = item.get("scopes") or {}
        scopes = AnchorScopes(
            mirrorgpt=bool(scopes_in.get("mirrorgpt", True)),
            echo_map=bool(scopes_in.get("echo_map", False)),
            echo_vault=bool(scopes_in.get("echo_vault", False)),
            legacy_capsule=bool(scopes_in.get("legacy_capsule", False)),
        )
        return cls(
            user_id=str(item.get("user_id", "")),
            anchor_id=str(item.get("anchor_id", "")),
            anchor_type=str(item.get("anchor_type", "custom")),
            title=str(item.get("title", "")),
            description=str(item.get("description", "")),
            relationship=item.get("relationship"),
            date=item.get("date"),
            emotional_weight=str(item.get("emotional_weight", "medium")),
            reflection_use=str(item.get("reflection_use", "when_relevant")),
            status=str(item.get("status", "active")),
            scopes=scopes,
            tone_guidance=list(item.get("tone_guidance") or []),
            created_from=str(item.get("created_from", "mirrorgpt")),
            user_confirmed=bool(item.get("user_confirmed", True)),
            created_at=str(item.get("created_at") or _utcnow_iso()),
            updated_at=str(item.get("updated_at") or _utcnow_iso()),
        )
