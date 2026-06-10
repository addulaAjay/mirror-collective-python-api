"""
Soul Ping API — user-facing config + a dev/manual test trigger.

- GET  /api/soul-pings/preferences  → current config (enabled + categories)
- PUT  /api/soul-pings/preferences  → update which categories are enabled
- POST /api/soul-pings/test         → send one ping to yourself now (bypasses
                                       only the hourly throttle, never config)

Categories are stored on the user's profile under
``preferences["soul_pings"]`` so no schema migration is needed.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core.security import get_current_user
from ..models.soul_ping import V1_CATEGORIES, SoulPingCategory
from ..services.dynamodb_service import get_dynamodb_service
from ..services.soul_ping_service import get_soul_ping_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/soul-pings", tags=["soul-pings"])

_ALLOWED = {c.value for c in V1_CATEGORIES}


class SoulPingPreferences(BaseModel):
    enabled: bool = True
    categories: List[str] = Field(
        default_factory=lambda: [c.value for c in V1_CATEGORIES]
    )


def _uid(current_user: Dict[str, Any]) -> str:
    user_id = current_user.get("id") or current_user.get("sub", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthenticated")
    return user_id


@router.get("/preferences", response_model=SoulPingPreferences)
async def get_preferences(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> SoulPingPreferences:
    db = get_dynamodb_service()
    profile = await db.get_user_profile(_uid(current_user))
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    config = get_soul_ping_service().get_config(profile)
    return SoulPingPreferences(**config)


@router.put("/preferences", response_model=SoulPingPreferences)
async def update_preferences(
    payload: SoulPingPreferences,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> SoulPingPreferences:
    # Validate categories against the v1 set; reject unknowns explicitly so a
    # typo doesn't silently disable a category.
    invalid = [c for c in payload.categories if c not in _ALLOWED]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown categories: {invalid}. Allowed: {sorted(_ALLOWED)}",
        )

    db = get_dynamodb_service()
    user_id = _uid(current_user)
    profile = await db.get_user_profile(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    prefs = dict(profile.preferences or {})
    # De-dupe while preserving order.
    seen: set[str] = set()
    categories: List[str] = []
    for c in payload.categories:
        if c not in seen:
            seen.add(c)
            categories.append(c)
    prefs["soul_pings"] = {"enabled": payload.enabled, "categories": categories}
    profile.preferences = prefs
    await db.update_user_profile(profile)

    return SoulPingPreferences(enabled=payload.enabled, categories=categories)


class TestPingResponse(BaseModel):
    status: str
    reason: Optional[str] = None
    category: Optional[str] = None
    endpoints: int = 0


@router.post("/test", response_model=TestPingResponse)
async def send_test_ping(
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> TestPingResponse:
    """Generate + send one Soul Ping to the caller now (ignores the throttle).

    Useful for end-to-end verification on a real device. Still respects the
    user's enabled config and requires a registered device + conversation
    history to produce content.
    """
    result = await get_soul_ping_service().maybe_send_for_user(
        _uid(current_user), source="manual", force=True
    )
    return TestPingResponse(
        status=result.status,
        reason=result.reason,
        category=result.category,
        endpoints=result.endpoints,
    )
