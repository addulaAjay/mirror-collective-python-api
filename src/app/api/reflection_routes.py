"""Reflection Room V1 — quiz + room override routes (spec §6.1, §6.5).

Routes:
  POST /reflection/quiz       — score quiz, assign motif, seed loops
  PUT  /me/reflection/room    — apply room-skin override

Inline Pydantic models follow existing repo convention (see echo_routes.py).
The response envelope ``{success, data, message}`` matches the existing API.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from ..core.exceptions import (
    InvalidQuizAnswer,
    MotifNotFound,
    NotFoundError,
    OverrideNotAllowed,
)
from ..core.security import get_current_user
from ..models.reflection_session import ReflectionSession
from ..repositories.echo_loop_state_repo import EchoLoopStateRepo
from ..repositories.reflection_session_repo import ReflectionSessionRepo
from ..services.reflection import session_lifecycle
from ..services.reflection.loop_seeder import seed_loops_from_quiz
from ..services.reflection.motif_mapper import (
    MotifPayload,
    build_motif_payload,
    build_payload_from_session,
)
from ..services.reflection.quiz_rules_loader import load_quiz_rules
from ..services.reflection.quiz_scorer import score_quiz
from ..services.reflection.quiz_to_loop_seeding_loader import load_quiz_to_loop_seeding
from ..services.reflection.room_skin_resolver import resolve_override

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Reflection Room"])


# ============================================================
# Request / response models (spec §5.1)
# ============================================================


Q1Answer = Literal["curious", "grounded", "hopeful", "heavy", "scattered", "numb"]
Q2Answer = Literal["clarity", "peace", "healing", "inspiration", "stillness"]
Q3Answer = Literal[
    "compass",
    "mirror",
    "blocks",
    "spiral",
    "feather",
    "radiant_burst",
    "waves",
    "pyramid",
    "water_drop",
    "brick_stack",
    "sprout",
]
Q4Answer = Literal["soothing", "gentle", "insight", "direct", "presence"]


class QuizAnswers(BaseModel):
    q1: Q1Answer
    q2: Q2Answer
    q3: Q3Answer
    q4: Q4Answer


class QuizRequest(BaseModel):
    answers: QuizAnswers
    session_id: Optional[str] = None
    user_override_tag: Optional[str] = None  # tie-break override (spec §7 step 14)


class QuizResponseData(BaseModel):
    session_id: str = Field(..., description="UUIDv4 for the new or reused session")
    motif: MotifPayload
    tied_motifs: Optional[List[MotifPayload]] = Field(
        default=None,
        description=(
            "Populated only when override_allowed=true. Each entry is a full "
            "MotifPayload for one of the tied tags so the FE can render the "
            "override chooser without a second fetch."
        ),
    )


class RoomSkinOverrideRequest(BaseModel):
    motif_id: str  # must exist in motif_mapping.v1.json
    apply_to: Literal["session", "core_room"] = "session"


class RoomSkinOverrideData(BaseModel):
    session_id: str
    motif: MotifPayload
    applied_to: str


# ============================================================
# Dependency injection — repos can be overridden in tests
# ============================================================


def get_reflection_session_repo() -> ReflectionSessionRepo:
    return ReflectionSessionRepo()


def get_echo_loop_state_repo() -> EchoLoopStateRepo:
    return EchoLoopStateRepo()


# ============================================================
# Helpers
# ============================================================


def _quiz_answers_dict(answers: QuizAnswers) -> Dict[str, str]:
    return {"q1": answers.q1, "q2": answers.q2, "q3": answers.q3, "q4": answers.q4}


async def _seed_loops_for_session(
    user_id: str,
    answers: Dict[str, str],
    repo: EchoLoopStateRepo,
) -> None:
    """Seed mc_echo_loop_state from quiz answers (spec §8.3 reseed path).

    Wipes any prior loop rows for the user, then upserts the fresh seeds. This
    is used when:
      * a brand-new session is created
      * an active session is overwritten by different answers
    Same-answers reuse skips this entirely.
    """
    seeding_cfg = load_quiz_to_loop_seeding()
    seeds = seed_loops_from_quiz(answers, seeding_cfg)

    # Wipe prior — fresh seeding overwrites existing loop state per spec §8.3.
    await repo.delete_for_user(user_id)
    if not seeds:
        return
    now_iso = session_lifecycle.iso(session_lifecycle.now_utc())
    states = [
        s.to_loop_state(user_id=user_id, last_seen_iso=now_iso, updated_at_iso=now_iso)
        for s in seeds
    ]
    await repo.upsert_many(states)


def _build_tied_payloads(
    tied_tags: List[str],
    scores: Dict[str, int],
    explanation: List[str],
) -> List[MotifPayload]:
    """One MotifPayload per tied tag, all marked override_allowed=True."""
    return [
        build_motif_payload(
            tag, scores=scores, explanation=explanation, override_allowed=True
        )
        for tag in tied_tags
    ]


# ============================================================
# POST /reflection/quiz
# ============================================================


@router.post(
    "/reflection/quiz",
    response_model=Dict[str, Any],
    status_code=200,
    summary="Score the Reflection Quiz, assign motif, seed loop state",
    description=(
        "Spec §6.1. Same answers within an active session reuse the existing "
        "session; different answers overwrite the session in place; expired "
        "sessions create a new one. Sets initial loop state via the seeder "
        "(spec §8.3)."
    ),
)
async def submit_quiz(
    request: QuizRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    x_user_timezone: Optional[str] = Header(default=None, alias="X-User-Timezone"),
    sessions: ReflectionSessionRepo = Depends(get_reflection_session_repo),
    loop_states: EchoLoopStateRepo = Depends(get_echo_loop_state_repo),
):
    """Score quiz, resolve motif, seed/refresh loop state.

    Spec §6.1 reuse rules:
      * No active session → create new (full seed).
      * Active session + same answers + same motif → reuse (no reseed).
      * Active session + different answers → overwrite same session_id (full reseed).
    """
    user_id = current_user.get("id") or current_user.get("sub")
    if not user_id:
        raise InvalidQuizAnswer("authenticated user has no id/sub claim")

    answers_dict = _quiz_answers_dict(request.answers)

    # --- Score the quiz ---
    rules = load_quiz_rules()
    scoring = score_quiz(
        answers_dict, rules, user_override_tag=request.user_override_tag
    )
    motif_payload = build_motif_payload(
        scoring.winning_tag,
        scores=scoring.scores,
        explanation=scoring.explanation,
        override_allowed=scoring.override_allowed,
    )

    # --- Reuse vs overwrite vs create ---
    latest = await sessions.get_latest_for_user(user_id)
    now = session_lifecycle.now_utc()

    if latest is not None and session_lifecycle.is_active(latest, now):
        # Active session exists. Reuse only if both answers AND chosen motif match.
        same_answers = latest.quiz_answers == answers_dict
        same_motif = latest.motif_id == motif_payload.motif_id
        if same_answers and same_motif:
            response_payload = build_payload_from_session(
                motif_id=latest.motif_id,
                scores=latest.scores or scoring.scores,
                explanation=scoring.explanation,
                override_allowed=bool(
                    (latest.motif_payload or {}).get("override_allowed", False)
                ),
            )
            data = QuizResponseData(
                session_id=latest.session_id,
                motif=response_payload,
                tied_motifs=None,
            )
            return _quiz_response(
                data, scoring.tied_tags, scoring.scores, scoring.explanation
            )

        # Different answers (or different override) → overwrite same session_id.
        await sessions.update_motif_and_quiz(
            session_id=latest.session_id,
            motif_id=motif_payload.motif_id,
            motif_name=motif_payload.motif_name,
            room_skin=motif_payload.room_skin,
            motif_payload=motif_payload.model_dump(),
            quiz_answers=answers_dict,
            scores=scoring.scores,
        )
        await _seed_loops_for_session(user_id, answers_dict, loop_states)
        data = QuizResponseData(
            session_id=latest.session_id,
            motif=motif_payload,
            tied_motifs=(
                _build_tied_payloads(
                    scoring.tied_tags, scoring.scores, scoring.explanation
                )
                if scoring.override_allowed
                else None
            ),
        )
        return _quiz_response(
            data, scoring.tied_tags, scoring.scores, scoring.explanation
        )

    # --- Brand-new session ---
    user_tz = session_lifecycle.resolve_user_tz(x_user_timezone)
    created_at, expires_at, ttl = session_lifecycle.compute_session_window(user_tz, now)
    session = ReflectionSession(
        user_id=user_id,
        motif_id=motif_payload.motif_id,
        motif_name=motif_payload.motif_name,
        room_skin=motif_payload.room_skin,
        motif_payload=motif_payload.model_dump(),
        quiz_answers=answers_dict,
        scores=scoring.scores,
        user_tz=user_tz,
        expires_at=expires_at,
        created_at=created_at,
        updated_at=created_at,
        ttl=ttl,
    )
    await sessions.put(session)
    await _seed_loops_for_session(user_id, answers_dict, loop_states)

    data = QuizResponseData(
        session_id=session.session_id,
        motif=motif_payload,
        tied_motifs=(
            _build_tied_payloads(scoring.tied_tags, scoring.scores, scoring.explanation)
            if scoring.override_allowed
            else None
        ),
    )
    return _quiz_response(data, scoring.tied_tags, scoring.scores, scoring.explanation)


def _quiz_response(
    data: QuizResponseData,
    tied_tags: List[str],
    scores: Dict[str, int],
    explanation: List[str],
) -> Dict[str, Any]:
    return {
        "success": True,
        "data": data.model_dump(exclude_none=True),
        "message": "Reflection quiz scored successfully",
    }


# ============================================================
# PUT /me/reflection/room
# ============================================================


@router.put(
    "/me/reflection/room",
    response_model=Dict[str, Any],
    status_code=200,
    summary="Override the active session's room skin",
    description=(
        "Spec §6.5. Only valid when the active session's quiz produced a tie "
        "(``override_allowed=True`` on the stored motif_payload)."
    ),
)
async def override_room_skin(
    request: RoomSkinOverrideRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    sessions: ReflectionSessionRepo = Depends(get_reflection_session_repo),
):
    """Apply a room-skin override.

    ``apply_to=session`` updates only the active session's override field.
    ``apply_to=core_room`` would persist a user-level default — out of V1
    scope (UserProfile has no field for this yet); for V1 it falls back to
    session-only behavior with a warning logged.
    """
    user_id = current_user.get("id") or current_user.get("sub")
    if not user_id:
        raise InvalidQuizAnswer("authenticated user has no id/sub claim")

    latest = await sessions.get_latest_for_user(user_id)
    if latest is None:
        raise NotFoundError("no active reflection session for user")

    # Validate override + look up target motif (raises MotifNotFound or OverrideNotAllowed).
    target = resolve_override(latest, request.motif_id)

    if request.apply_to == "core_room":
        # V1: log + apply at session level. UserProfile core-room field is V2.
        logger.warning(
            "apply_to=core_room requested but not yet supported; applying at session level"
        )

    updated = await sessions.update_room_skin(latest.session_id, target.room_skin)
    if updated is None:
        raise NotFoundError("session disappeared between read and write")

    payload = build_payload_from_session(
        motif_id=target.motif_id,
        scores=updated.scores or {},
        explanation=[],
        override_allowed=False,  # consumed
    )
    data = RoomSkinOverrideData(
        session_id=updated.session_id,
        motif=payload,
        applied_to=request.apply_to,
    )
    return {
        "success": True,
        "data": data.model_dump(),
        "message": "Room skin override applied",
    }
