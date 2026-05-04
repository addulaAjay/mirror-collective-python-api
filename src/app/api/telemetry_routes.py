"""Reflection Room V1 — telemetry beacon endpoints (spec §10).

These endpoints exist purely so the FE can fire client→server telemetry
beacons that don't fit the request/response shape of the main routes.

Routes:
  POST /telemetry/practice-expand   — fires when a card back opens
  POST /telemetry/nudge-opened      — fires when an external nudge expands
  POST /telemetry/echo-map-refresh  — fires when "Update My Mirror" is tapped

Each emits exactly one event from the spec §10 matrix and returns 204.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from ..core.security import get_current_user
from ..services.telemetry.reflection_events import (
    EVENT_ECHO_MAP_REFRESH,
    EVENT_NUDGE_OPENED,
    EVENT_PRACTICE_EXPAND,
    TelemetryEmitter,
    get_default_emitter,
    hash_user_id,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Telemetry"])


LoopId = Literal[
    "pressure",
    "overwhelm",
    "grief",
    "self_silencing",
    "agency",
    "transition",
]


class PracticeExpandBeacon(BaseModel):
    loop_id: LoopId
    practice_id: str


class NudgeOpenedBeacon(BaseModel):
    nudge_type: str  # short identifier, ≤64 chars (PII filter trims)


def get_telemetry_emitter() -> TelemetryEmitter:
    return get_default_emitter()


def _user_hash_or_401(user: Dict[str, Any]) -> str:
    user_id = user.get("id") or user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="missing user id in claims")
    return hash_user_id(user_id)


@router.post(
    "/telemetry/practice-expand",
    status_code=204,
    summary="Beacon: user opened a practice card back",
    description="Emits practice_expand (spec §10).",
)
async def beacon_practice_expand(
    request: PracticeExpandBeacon,
    current_user: Dict[str, Any] = Depends(get_current_user),
    telemetry: TelemetryEmitter = Depends(get_telemetry_emitter),
):
    """Fired when the user opens the back of a practice card."""
    telemetry.emit(
        EVENT_PRACTICE_EXPAND,
        user_hash=_user_hash_or_401(current_user),
        loop_id=request.loop_id,
        practice_id=request.practice_id,
    )
    return Response(status_code=204)


@router.post(
    "/telemetry/nudge-opened",
    status_code=204,
    summary="Beacon: user expanded a nudge",
    description="Emits nudge_opened (spec §10).",
)
async def beacon_nudge_opened(
    request: NudgeOpenedBeacon,
    current_user: Dict[str, Any] = Depends(get_current_user),
    telemetry: TelemetryEmitter = Depends(get_telemetry_emitter),
):
    """Fired when an external nudge is expanded by the user."""
    telemetry.emit(
        EVENT_NUDGE_OPENED,
        user_hash=_user_hash_or_401(current_user),
        nudge_type=request.nudge_type,
    )
    return Response(status_code=204)


@router.post(
    "/telemetry/echo-map-refresh",
    status_code=204,
    summary="Beacon: user tapped 'Update My Mirror'",
    description="Emits echo_map_refresh (spec §10).",
)
async def beacon_echo_map_refresh(
    current_user: Dict[str, Any] = Depends(get_current_user),
    telemetry: TelemetryEmitter = Depends(get_telemetry_emitter),
):
    """Fired when the user taps 'Update My Mirror' on the Echo Map."""
    telemetry.emit(
        EVENT_ECHO_MAP_REFRESH,
        user_hash=_user_hash_or_401(current_user),
    )
    return Response(status_code=204)
