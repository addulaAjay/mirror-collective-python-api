"""Reflection Room V1 — user preference + privacy routes (spec §10.1).

Routes:
  GET  /me/preferences                  — read flags + disallow_types
  PUT  /me/preferences/flags            — update one or more flags
  POST /me/private-mode/reveal          — telemetry beacon when private mode
                                          content is revealed

The flags object drives behavior server-side:
  * ``no_breathwork``  → safety filter drops type=breath candidates (Phase 5)
  * ``reduced_motion`` → no backend impact; FE consumes the flag directly
  * ``private_mode``   → FE blurs content until tap; backend echoes the flag
                          in recommend-practice responses (Phase 5)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ..core.security import get_current_user
from ..repositories.user_personalization_repo import UserPersonalizationRepo
from ..services.telemetry.reflection_events import (
    EVENT_PRIVATE_MODE_REVEAL,
    TelemetryEmitter,
    get_default_emitter,
    hash_user_id,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["User Preferences"])


# ============================================================
# Pydantic models
# ============================================================


RevealSurface = Literal["echo_signature", "mirror_moment", "chat"]


class FlagsOut(BaseModel):
    no_breathwork: bool = False
    reduced_motion: bool = False
    private_mode: bool = False


class PreferencesOut(BaseModel):
    flags: FlagsOut
    disallow_types: List[str] = Field(default_factory=list)


class UpdateFlagsRequest(BaseModel):
    no_breathwork: Optional[bool] = None
    reduced_motion: Optional[bool] = None
    private_mode: Optional[bool] = None


class PrivateModeRevealBeacon(BaseModel):
    surface: RevealSurface


# ============================================================
# Dependency injection
# ============================================================


def get_user_personalization_repo() -> UserPersonalizationRepo:
    return UserPersonalizationRepo()


def get_telemetry_emitter() -> TelemetryEmitter:
    return get_default_emitter()


def _user_id_or_401(user: Dict[str, Any]) -> str:
    user_id = user.get("id") or user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="missing user id in claims")
    return user_id


# ============================================================
# GET /me/preferences
# ============================================================


@router.get(
    "/me/preferences",
    response_model=Dict[str, Any],
    summary="Read user flags + disallow_types",
    description="Returns no_breathwork / reduced_motion / private_mode flags and any per-user disallow_types. New users default to all flags false.",
)
async def get_preferences(
    current_user: Dict[str, Any] = Depends(get_current_user),
    prefs: UserPersonalizationRepo = Depends(get_user_personalization_repo),
):
    """Return the user's flags + disallow_types."""
    user_id = _user_id_or_401(current_user)
    record = await prefs.get_or_default(user_id)
    out = PreferencesOut(
        flags=FlagsOut(
            no_breathwork=record.flags.no_breathwork,
            reduced_motion=record.flags.reduced_motion,
            private_mode=record.flags.private_mode,
        ),
        disallow_types=list(record.disallow_types or []),
    )
    return {
        "success": True,
        "data": out.model_dump(),
        "message": "Preferences loaded",
    }


# ============================================================
# PUT /me/preferences/flags
# ============================================================


@router.put(
    "/me/preferences/flags",
    response_model=Dict[str, Any],
    summary="Update one or more user flags (partial update)",
    description="Omitted fields are left unchanged. no_breathwork drives the recommender's safety filter (spec §9.3).",
)
async def update_flags(
    request: UpdateFlagsRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    prefs: UserPersonalizationRepo = Depends(get_user_personalization_repo),
):
    """Partial update of user flags. Omitted fields are left unchanged."""
    user_id = _user_id_or_401(current_user)
    updated = await prefs.set_flags(
        user_id,
        no_breathwork=request.no_breathwork,
        reduced_motion=request.reduced_motion,
        private_mode=request.private_mode,
    )
    out = FlagsOut(
        no_breathwork=updated.flags.no_breathwork,
        reduced_motion=updated.flags.reduced_motion,
        private_mode=updated.flags.private_mode,
    )
    return {
        "success": True,
        "data": out.model_dump(),
        "message": "Flags updated",
    }


# ============================================================
# POST /me/private-mode/reveal — telemetry beacon (spec §10.1)
# ============================================================


@router.post(
    "/me/private-mode/reveal",
    status_code=204,
    summary="Beacon: user revealed private-mode content",
    description="Emits private_mode_reveal (spec §10.1). Used to measure how often Private Mode is engaged with.",
)
async def private_mode_reveal(
    request: PrivateModeRevealBeacon,
    current_user: Dict[str, Any] = Depends(get_current_user),
    telemetry: TelemetryEmitter = Depends(get_telemetry_emitter),
):
    """Fires the ``private_mode_reveal`` event when the user taps to reveal
    private-mode content. Useful for measuring how often Private Mode is
    actually engaged with."""
    user_id = _user_id_or_401(current_user)
    telemetry.emit(
        EVENT_PRIVATE_MODE_REVEAL,
        user_hash=hash_user_id(user_id),
        surface=request.surface,
    )
    return Response(status_code=204)
