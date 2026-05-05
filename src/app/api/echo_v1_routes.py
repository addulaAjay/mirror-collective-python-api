"""Echo Snapshot V1 + Practice Recommendation routes.

Routes:
  GET  /echo/snapshot                — Phase 4 (this file)
  POST /echo/recommend-practice      — Phase 5 (extended later)
  POST /dev/echo/loop-state          — dev-only QA seeder (Phase 4)

Filename ``echo_v1_routes.py`` avoids colliding with the existing
``echo_routes.py`` (Echo Vault). Mounted at ``/api`` prefix in handler.py.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, confloat, conint

from ..core.exceptions import AllCandidatesFiltered, FallbackOnCooldown
from ..core.security import get_current_user
from ..models.echo_loop_state import EchoLoopState
from ..repositories.echo_loop_state_repo import EchoLoopStateRepo
from ..repositories.practice_completion_repo import PracticeCompletionRepo
from ..repositories.reflection_session_repo import ReflectionSessionRepo
from ..repositories.user_personalization_repo import UserPersonalizationRepo
from ..services.echo.snapshot_service import V1_SUPPORTED_LOOPS, build_snapshot
from ..services.practice.recommender import recommend
from ..services.telemetry.reflection_events import (
    EVENT_ECHO_SIGNATURE_VIEW,
    TelemetryEmitter,
    get_default_emitter,
    hash_user_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Echo Snapshot V1"])


# ============================================================
# Pydantic models (spec §5.2)
# ============================================================


LoopId = Literal[
    "pressure",
    "overwhelm",
    "grief",
    "self_silencing",
    "agency",
    "transition",
]
ToneState = Literal["rising", "steady", "softening"]
IntensityLabel = Literal["High", "Medium", "Low"]


class LoopStateOut(BaseModel):
    """Spec §5.2."""

    loop_id: LoopId
    tone_state: ToneState
    intensity_score: confloat(ge=0.0, le=1.0)
    intensity_label: IntensityLabel
    last_seen: str
    recently_changed: bool = False
    narrative_stage: Optional[str] = None
    icon: Optional[str] = None
    reflection_line: Optional[str] = None


class MotifContext(BaseModel):
    motif_id: str
    room_skin: str


class SnapshotOut(BaseModel):
    """Spec §5.2 — body of GET /echo/snapshot."""

    session_id: str
    motif_context: MotifContext
    loops: List[LoopStateOut] = Field(default_factory=list)
    updated_at: str


# ============================================================
# Practice recommendation models (spec §5.2, §6.3)
# ============================================================


PracticeType = Literal["breath", "somatic", "cognitive", "action", "reflection"]
RecommendSurface = Literal["echo_signature", "mirror_moment", "chat"]


class RecommendPracticeRequest(BaseModel):
    session_id: str
    selected_loop: Optional[LoopId] = None
    surface: RecommendSurface = "echo_signature"


class PatternInfoOut(BaseModel):
    loop_id: LoopId
    strength: confloat(ge=0.0, le=1.0)
    trend: ToneState
    last_seen: str


class PracticePayloadOut(BaseModel):
    id: str
    title: str
    type: PracticeType
    duration_sec: conint(ge=0)
    steps: List[str]


class RecommendPracticeResponse(BaseModel):
    pattern: PatternInfoOut
    practice: PracticePayloadOut
    rule_id: str
    private_mode_active: bool = False


# ============================================================
# Dev-only seeding payload
# ============================================================


class DevLoopStateRow(BaseModel):
    """Single row in the dev seeder payload — mirrors EchoLoopState fields."""

    loop_id: LoopId
    tone_state: ToneState
    intensity_score: confloat(ge=0.0, le=1.0)
    intensity_label: Optional[IntensityLabel] = None
    last_seen: Optional[str] = None
    recently_changed: bool = True
    narrative_stage: Optional[str] = None


class DevSeedRequest(BaseModel):
    """Replaces the user's full loop-state set with the supplied rows."""

    loops: List[DevLoopStateRow]


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


def _is_production() -> bool:
    """Spec §8.3 dev-endpoint gate. Anything other than 'production' enables it."""
    return os.getenv("ENVIRONMENT", "development").lower() == "production"


def _label_from_score(score: float) -> str:
    if score >= 0.66:
        return "High"
    if score >= 0.33:
        return "Medium"
    return "Low"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ============================================================
# GET /echo/snapshot
# ============================================================


@router.get(
    "/echo/snapshot",
    response_model=Dict[str, Any],
    summary="Get echo snapshot for current session",
    description=(
        "Returns active loop state for the user's most recent session (or "
        "the session given by ``session_id``). Loops are sorted by "
        "intensity_score desc and enriched with icon + reflection_line. "
        "Emits ``echo_signature_view`` (spec §10) on 200."
    ),
)
async def get_snapshot(
    session_id: Optional[str] = Query(
        default=None,
        description=(
            "Explicit session to read. If omitted, the server returns the "
            "user's most recent session."
        ),
    ),
    current_user: Dict[str, Any] = Depends(get_current_user),
    sessions: ReflectionSessionRepo = Depends(get_reflection_session_repo),
    loop_states: EchoLoopStateRepo = Depends(get_echo_loop_state_repo),
    telemetry: TelemetryEmitter = Depends(get_telemetry_emitter),
):
    """Return active loop state for the user's current session (spec §6.2).

    Loops are sorted by ``intensity_score`` desc. Each loop is enriched with
    ``icon`` + ``reflection_line`` from the tone library so the FE doesn't
    need a second fetch. Emits ``echo_signature_view`` on 200 (spec §10).
    """
    user_id = current_user.get("id") or current_user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="missing user id in claims")

    snapshot = await build_snapshot(
        user_id=user_id,
        session_id=session_id,
        sessions_repo=sessions,
        loop_state_repo=loop_states,
    )

    telemetry.emit(
        EVENT_ECHO_SIGNATURE_VIEW,
        user_hash=hash_user_id(user_id),
        loops_count=len(snapshot.loops),
        motif_id=snapshot.motif_id,
    )

    out = SnapshotOut(
        session_id=snapshot.session_id,
        motif_context=MotifContext(
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
    return {
        "success": True,
        "data": out.model_dump(),
        "message": "Snapshot built successfully",
    }


# ============================================================
# POST /dev/echo/loop-state — dev-only QA seeding (spec §8.3)
# ============================================================


@router.post(
    "/dev/echo/loop-state",
    response_model=Dict[str, Any],
    summary="(Dev only) Seed loop state for QA",
    description=(
        "Replaces the user's full loop_state set. Returns 404 in production "
        "(``ENVIRONMENT=production``). Use case: FE / QA needs to test all "
        "(loop × tone) combinations without rigging quiz inputs."
    ),
)
async def dev_seed_loop_state(
    request: DevSeedRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    loop_states: EchoLoopStateRepo = Depends(get_echo_loop_state_repo),
):
    """Replace the user's full loop_state set. Disabled in production.

    Use case: FE / QA needs to test all (loop × tone) combinations without
    rigging the quiz inputs. The endpoint wipes existing rows and writes the
    supplied ones; loop_id values must be in the V1 supported set.
    """
    if _is_production():
        raise HTTPException(status_code=404, detail="not found")

    user_id = current_user.get("id") or current_user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="missing user id in claims")

    now_iso = _now_iso()
    states: List[EchoLoopState] = []
    for row in request.loops:
        if row.loop_id not in V1_SUPPORTED_LOOPS:
            raise HTTPException(
                status_code=400, detail=f"loop_id '{row.loop_id}' not supported"
            )
        states.append(
            EchoLoopState(
                user_id=user_id,
                loop_id=row.loop_id,
                tone_state=row.tone_state,
                intensity_score=float(row.intensity_score),
                intensity_label=row.intensity_label
                or _label_from_score(float(row.intensity_score)),
                last_seen=row.last_seen or now_iso,
                recently_changed=row.recently_changed,
                narrative_stage=row.narrative_stage,
                updated_at=now_iso,
            )
        )

    await loop_states.delete_for_user(user_id)
    if states:
        await loop_states.upsert_many(states)

    return {
        "success": True,
        "data": {"seeded": len(states), "user_id": user_id},
        "message": "Loop state seeded for development",
    }


# ============================================================
# POST /echo/recommend-practice (spec §6.3)
# ============================================================


@router.post(
    "/echo/recommend-practice",
    response_model=Dict[str, Any],
    summary="Recommend a practice for an active loop",
    description=(
        "Returns one ranked 1–2 minute practice (spec §6.3). With V1 default "
        "fallback_enabled=true, callers see at most:\n\n"
        "* 200 — practice payload\n"
        "* 400 LOOP_NOT_SUPPORTED — selected_loop outside V1 set\n"
        "* 404 NO_ACTIVE_LOOPS — selected_loop=null and nothing active\n"
        "* 409 FALLBACK_ON_COOLDOWN — Retry-After header included"
    ),
)
async def recommend_practice(
    request: RecommendPracticeRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    sessions: ReflectionSessionRepo = Depends(get_reflection_session_repo),
    loop_states: EchoLoopStateRepo = Depends(get_echo_loop_state_repo),
    completions: PracticeCompletionRepo = Depends(get_practice_completion_repo),
    prefs: UserPersonalizationRepo = Depends(get_user_personalization_repo),
):
    """Return one ranked 1-2 minute practice for an active loop."""
    user_id = current_user.get("id") or current_user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="missing user id in claims")

    # AllCandidatesFiltered + FallbackOnCooldown propagate to the central
    # error handler, which emits the envelope with errorCode + Retry-After
    # header automatically (see error_handlers.py).
    result = await recommend(
        user_id=user_id,
        session_id=request.session_id,
        selected_loop=request.selected_loop,
        surface=request.surface,
        sessions_repo=sessions,
        loop_state_repo=loop_states,
        completions_repo=completions,
        prefs_repo=prefs,
    )

    response_body = RecommendPracticeResponse(
        pattern=PatternInfoOut(
            loop_id=result.pattern.loop_id,
            strength=result.pattern.strength,
            trend=result.pattern.trend,
            last_seen=result.pattern.last_seen,
        ),
        practice=PracticePayloadOut(
            id=result.practice.id,
            title=result.practice.title,
            type=result.practice.type,
            duration_sec=result.practice.duration_sec,
            steps=list(result.practice.steps),
        ),
        rule_id=result.rule_id,
        private_mode_active=result.private_mode_active,
    )
    return {
        "success": True,
        "data": response_body.model_dump(),
        "message": "Practice recommended successfully",
    }
