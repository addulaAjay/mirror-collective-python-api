"""Snapshot builder (spec §8.1).

Fetches the user's active loop-state rows, filters out:
  * unsupported loop_ids (forward-compat guard, spec §9.2 step 26)
  * fully-resolved loops at ``intensity_score == 0.0`` (faded from snapshot
    until the next quiz reseed; spec §8.3 edge case)

Enriches each surviving row with ``icon`` + ``reflection_line`` from the tone
library (spec §6.2) so the FE doesn't need a second fetch. Sorts by
``intensity_score`` desc.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from ...core.exceptions import NotFoundError
from ...models.echo_loop_state import EchoLoopState
from ...models.reflection_session import ReflectionSession
from ...repositories.echo_loop_state_repo import EchoLoopStateRepo
from ...repositories.reflection_session_repo import ReflectionSessionRepo
from .tone_library_loader import load_tone_library

V1_SUPPORTED_LOOPS = frozenset(
    {"pressure", "overwhelm", "grief", "self_silencing", "agency", "transition"}
)


class EnrichedLoopState:
    """Snapshot-shaped row.

    A plain object (not a Pydantic model) so route handlers can serialize via
    ``model_dump`` on a `LoopStateOut` Pydantic model OR consume the dataclass
    directly. Equivalent to the spec §5.2 ``LoopState`` shape.
    """

    __slots__ = (
        "loop_id",
        "tone_state",
        "intensity_score",
        "intensity_label",
        "last_seen",
        "recently_changed",
        "narrative_stage",
        "icon",
        "reflection_line",
    )

    def __init__(
        self,
        *,
        loop_id: str,
        tone_state: str,
        intensity_score: float,
        intensity_label: str,
        last_seen: str,
        recently_changed: bool,
        narrative_stage: Optional[str],
        icon: str,
        reflection_line: str,
    ):
        self.loop_id = loop_id
        self.tone_state = tone_state
        self.intensity_score = intensity_score
        self.intensity_label = intensity_label
        self.last_seen = last_seen
        self.recently_changed = recently_changed
        self.narrative_stage = narrative_stage
        self.icon = icon
        self.reflection_line = reflection_line


class Snapshot:
    """Snapshot-shaped response object (motif_context + loops + updated_at).

    Also carries the session's ``user_tz`` so downstream services (e.g. the
    personalizer's time-of-day bucket) can compute in the user's local time
    without re-fetching the session.
    """

    def __init__(
        self,
        *,
        session_id: str,
        motif_id: str,
        room_skin: str,
        loops: List[EnrichedLoopState],
        updated_at: str,
        user_tz: str,
    ):
        self.session_id = session_id
        self.motif_id = motif_id
        self.room_skin = room_skin
        self.loops = loops
        self.updated_at = updated_at
        self.user_tz = user_tz


async def build_snapshot(
    user_id: str,
    session_id: Optional[str],
    *,
    sessions_repo: ReflectionSessionRepo,
    loop_state_repo: EchoLoopStateRepo,
) -> Snapshot:
    """Spec §8.1 algorithm.

    Args:
        user_id: Cognito ``sub`` of the authenticated user.
        session_id: Explicit session to read; if None, the user's latest is used.
        sessions_repo / loop_state_repo: injected for testability.

    Raises:
        NotFoundError: invalid or unknown ``session_id`` (or no session at all).
    """
    session = await _resolve_session(user_id, session_id, sessions_repo)

    rows = await loop_state_repo.query_by_user(user_id)
    tone_library = load_tone_library()

    enriched: List[EnrichedLoopState] = []
    for row in rows:
        if row.loop_id not in V1_SUPPORTED_LOOPS:
            continue  # forward-compat guard
        if float(row.intensity_score) <= 0.0:
            continue  # fully-resolved fades from snapshot until reseed
        try:
            tone_entry = tone_library.lookup(row.loop_id, row.tone_state)
        except KeyError:
            # Defensive: unknown loop/tone in storage shouldn't crash the snapshot.
            continue
        enriched.append(
            EnrichedLoopState(
                loop_id=row.loop_id,
                tone_state=row.tone_state,
                intensity_score=float(row.intensity_score),
                intensity_label=row.intensity_label,
                last_seen=row.last_seen,
                recently_changed=bool(row.recently_changed),
                narrative_stage=row.narrative_stage,
                icon=tone_entry.icon,
                reflection_line=tone_entry.reflection_line,
            )
        )

    enriched.sort(key=lambda l: l.intensity_score, reverse=True)

    return Snapshot(
        session_id=session.session_id,
        motif_id=session.motif_id,
        room_skin=session.effective_room_skin(),
        loops=enriched,
        updated_at=_now_iso(),
        user_tz=session.user_tz or "America/New_York",
    )


async def _resolve_session(
    user_id: str,
    session_id: Optional[str],
    sessions_repo: ReflectionSessionRepo,
) -> ReflectionSession:
    if session_id:
        session = await sessions_repo.get(session_id)
        if session is None or session.user_id != user_id:
            # Don't leak that a session exists for another user.
            raise NotFoundError(f"session not found: {session_id}")
        return session

    latest = await sessions_repo.get_latest_for_user(user_id)
    if latest is None:
        raise NotFoundError("no reflection session for user")
    return latest


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
