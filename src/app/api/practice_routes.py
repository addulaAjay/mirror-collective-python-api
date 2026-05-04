"""Reflection Room V1 — practice completion routes (spec §6.4).

Routes:
  POST  /practice/complete                     — log completion + helpful vote
  PATCH /practice/complete/{completion_id}/helpful  — late helpful update
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, confloat

from ..core.exceptions import NotFoundError
from ..core.security import get_current_user
from ..models.practice_completion import PracticeCompletion
from ..repositories.echo_loop_state_repo import EchoLoopStateRepo
from ..repositories.practice_completion_repo import PracticeCompletionRepo
from ..repositories.reflection_session_repo import ReflectionSessionRepo
from ..repositories.user_personalization_repo import UserPersonalizationRepo
from ..services.echo.loop_state_updater import apply_completion_delta
from ..services.echo.snapshot_service import V1_SUPPORTED_LOOPS, build_snapshot
from ..services.practice.personalization_loader import load_personalization_defaults
from ..services.practice.personalizer import bucket_for_now
from ..services.telemetry.reflection_events import (
    EVENT_PRACTICE_COMPLETE,
    EVENT_PRACTICE_HELPFUL,
    EVENT_PRACTICE_NOT_HELPFUL,
    TelemetryEmitter,
    get_default_emitter,
    hash_user_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Practice"])


# ============================================================
# Pydantic models (spec §5.3)
# ============================================================


LoopId = Literal[
    "pressure", "overwhelm", "grief", "self_silencing", "agency", "transition"
]
ToneState = Literal["rising", "steady", "softening"]
IntensityLabel = Literal["High", "Medium", "Low"]


class CompletePracticeRequest(BaseModel):
    session_id: str
    loop_id: LoopId
    tone_state: ToneState
    practice_id: str
    rule_id: str
    helpful: Optional[bool] = None
    completed_at: Optional[str] = None


class LoopStateOut(BaseModel):
    loop_id: LoopId
    tone_state: ToneState
    intensity_score: confloat(ge=0.0, le=1.0)
    intensity_label: IntensityLabel
    last_seen: str
    recently_changed: bool = False
    narrative_stage: Optional[str] = None
    icon: Optional[str] = None
    reflection_line: Optional[str] = None


class MotifContextOut(BaseModel):
    motif_id: str
    room_skin: str


class SnapshotOut(BaseModel):
    session_id: str
    motif_context: MotifContextOut
    loops: List[LoopStateOut]
    updated_at: str


class CompletePracticeResponse(BaseModel):
    completion_id: str
    snapshot: SnapshotOut


class UpdateHelpfulRequest(BaseModel):
    helpful: bool


# ============================================================
# Dependency injection
# ============================================================


def get_reflection_session_repo() -> ReflectionSessionRepo:
    return ReflectionSessionRepo()


def get_echo_loop_state_repo() -> EchoLoopStateRepo:
    return EchoLoopStateRepo()


def get_practice_completion_repo() -> PracticeCompletionRepo:
    return PracticeCompletionRepo()


def get_user_personalization_repo() -> UserPersonalizationRepo:
    return UserPersonalizationRepo()


def get_telemetry_emitter() -> TelemetryEmitter:
    return get_default_emitter()


# ============================================================
# Helpers
# ============================================================


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


async def _build_snapshot_out(
    user_id: str,
    session_id: str,
    sessions: ReflectionSessionRepo,
    loop_states: EchoLoopStateRepo,
) -> SnapshotOut:
    snapshot = await build_snapshot(
        user_id=user_id,
        session_id=session_id,
        sessions_repo=sessions,
        loop_state_repo=loop_states,
    )
    return SnapshotOut(
        session_id=snapshot.session_id,
        motif_context=MotifContextOut(
            motif_id=snapshot.motif_id, room_skin=snapshot.room_skin
        ),
        loops=[
            LoopStateOut(
                loop_id=l.loop_id,
                tone_state=l.tone_state,
                intensity_score=l.intensity_score,
                intensity_label=l.intensity_label,
                last_seen=l.last_seen,
                recently_changed=l.recently_changed,
                narrative_stage=l.narrative_stage,
                icon=l.icon,
                reflection_line=l.reflection_line,
            )
            for l in snapshot.loops
        ],
        updated_at=snapshot.updated_at,
    )


# ============================================================
# POST /practice/complete
# ============================================================


@router.post(
    "/practice/complete",
    response_model=Dict[str, Any],
    summary="Log a practice completion + optional helpful vote",
    description=(
        "Spec §6.4. Inserts a completion row, updates personalization, "
        "applies the loop-state delta (spec §8.3), emits telemetry, and "
        "returns the refreshed snapshot inline."
    ),
)
async def complete_practice(
    request: CompletePracticeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    sessions: ReflectionSessionRepo = Depends(get_reflection_session_repo),
    loop_states: EchoLoopStateRepo = Depends(get_echo_loop_state_repo),
    completions: PracticeCompletionRepo = Depends(get_practice_completion_repo),
    prefs: UserPersonalizationRepo = Depends(get_user_personalization_repo),
    telemetry: TelemetryEmitter = Depends(get_telemetry_emitter),
):
    """Log a practice completion (spec §6.4). Side effects in order:
    1. Insert into ``mc_practice_completions``.
    2. Update ``mc_user_personalization`` (recent_use, time_of_day_history,
       optional helpfulness vote).
    3. Apply state delta to ``mc_echo_loop_state`` per spec §8.3.
    4. Emit telemetry events.
    5. Recompute and return the snapshot inline.
    """
    user_id = current_user.get("id") or current_user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="missing user id in claims")

    if request.loop_id not in V1_SUPPORTED_LOOPS:
        raise HTTPException(
            status_code=400, detail=f"loop_id '{request.loop_id}' not supported"
        )

    # Resolve session for tz + sanity (404 if it doesn't exist).
    session = await sessions.get(request.session_id)
    if session is None or session.user_id != user_id:
        raise NotFoundError(f"session not found: {request.session_id}")

    completed_dt = _parse_iso(request.completed_at) or datetime.now(timezone.utc)
    completed_iso = completed_dt.isoformat().replace("+00:00", "Z")

    # 1) Insert completion row.
    completion = PracticeCompletion(
        user_id=user_id,
        session_id=request.session_id,
        loop_id=request.loop_id,
        tone_state=request.tone_state,
        practice_id=request.practice_id,
        rule_id=request.rule_id,
        helpful=request.helpful,
        completed_at=completed_iso,
    )
    await completions.put(completion)

    # 2) Update user personalization.
    defaults = load_personalization_defaults()
    bucket = bucket_for_now(
        completed_dt,
        session.user_tz or "America/New_York",
        defaults.time_of_day_buckets,
    )
    await prefs.record_completion(
        user_id=user_id,
        practice_id=request.practice_id,
        time_of_day_bucket=bucket,
    )
    if request.helpful is not None:
        await prefs.record_helpfulness(
            user_id=user_id,
            practice_id=request.practice_id,
            helpful=request.helpful,
            ts=completed_iso,
        )

    # 3) Apply loop-state delta (spec §8.3).
    await apply_completion_delta(
        user_id=user_id,
        loop_id=request.loop_id,
        helpful=request.helpful,
        loop_state_repo=loop_states,
        completions_repo=completions,
        now=completed_dt,
    )

    # 4) Telemetry.
    user_hash = hash_user_id(user_id)
    telemetry.emit(
        EVENT_PRACTICE_COMPLETE,
        user_hash=user_hash,
        loop_id=request.loop_id,
        tone_state=request.tone_state,
        practice_id=request.practice_id,
        rule_id=request.rule_id,
    )
    if request.helpful is True:
        telemetry.emit(
            EVENT_PRACTICE_HELPFUL,
            user_hash=user_hash,
            practice_id=request.practice_id,
            rule_id=request.rule_id,
        )
    elif request.helpful is False:
        telemetry.emit(
            EVENT_PRACTICE_NOT_HELPFUL,
            user_hash=user_hash,
            practice_id=request.practice_id,
            rule_id=request.rule_id,
        )

    # 5) Refresh snapshot.
    snapshot_out = await _build_snapshot_out(
        user_id, request.session_id, sessions, loop_states
    )

    response = CompletePracticeResponse(
        completion_id=completion.completion_id, snapshot=snapshot_out
    )
    return {
        "success": True,
        "data": response.model_dump(),
        "message": "Practice completion logged",
    }


# ============================================================
# PATCH /practice/complete/{completion_id}/helpful
# ============================================================


@router.patch(
    "/practice/complete/{completion_id}/helpful",
    response_model=Dict[str, Any],
    summary="Late helpful vote for an existing completion",
    description=(
        "Spec §6.6. completion_id contains '#'; clients MUST URL-encode the "
        "path segment. Mirrors POST /practice/complete's downstream effects."
    ),
)
async def update_helpful(
    completion_id: str,
    request: UpdateHelpfulRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    sessions: ReflectionSessionRepo = Depends(get_reflection_session_repo),
    loop_states: EchoLoopStateRepo = Depends(get_echo_loop_state_repo),
    completions: PracticeCompletionRepo = Depends(get_practice_completion_repo),
    prefs: UserPersonalizationRepo = Depends(get_user_personalization_repo),
    telemetry: TelemetryEmitter = Depends(get_telemetry_emitter),
):
    """Late helpful vote (spec §6.6).

    Used when the user dismisses the helpfulness prompt during the initial
    POST and votes later. The state delta + personalization update fire here
    too, so the path mirrors the initial completion's downstream effects.

    **FE note:** ``completion_id`` is formatted as ``"<ts_iso>#<uuid>"`` per
    spec §3.3. Clients MUST URL-encode the segment (``#`` → ``%23``) before
    putting it in the path; otherwise the ``#`` is parsed as a URI fragment.
    """
    user_id = current_user.get("id") or current_user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="missing user id in claims")

    updated = await completions.update_helpful(
        user_id=user_id, completion_id=completion_id, helpful=request.helpful
    )
    if updated is None:
        raise NotFoundError(f"completion not found: {completion_id}")

    # Mirror the POST: record helpfulness, mutate loop state, emit telemetry.
    now = datetime.now(timezone.utc)
    await prefs.record_helpfulness(
        user_id=user_id,
        practice_id=updated.practice_id,
        helpful=request.helpful,
        ts=now.isoformat().replace("+00:00", "Z"),
    )
    await apply_completion_delta(
        user_id=user_id,
        loop_id=updated.loop_id,
        helpful=request.helpful,
        loop_state_repo=loop_states,
        completions_repo=completions,
        now=now,
    )
    user_hash = hash_user_id(user_id)
    telemetry.emit(
        EVENT_PRACTICE_HELPFUL if request.helpful else EVENT_PRACTICE_NOT_HELPFUL,
        user_hash=user_hash,
        practice_id=updated.practice_id,
        rule_id=updated.rule_id,
    )

    snapshot_out = await _build_snapshot_out(
        user_id, updated.session_id, sessions, loop_states
    )
    response = CompletePracticeResponse(
        completion_id=updated.completion_id, snapshot=snapshot_out
    )
    return {
        "success": True,
        "data": response.model_dump(),
        "message": "Helpfulness updated",
    }
