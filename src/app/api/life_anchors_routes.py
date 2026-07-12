"""Life Anchors — user-facing CRUD for durable, permissioned memory (Phase 2A).

Routes (all under /api, all scoped to the authenticated user via PK=user_id):
  GET    /me/life-anchors               — list all anchors
  POST   /me/life-anchors               — create an anchor
  PUT    /me/life-anchors/{anchor_id}   — partial update (edit/scope/reflection_use)
  POST   /me/life-anchors/{anchor_id}/pause  — pause (stop using in reflections)
  DELETE /me/life-anchors/{anchor_id}   — hard-delete an anchor

A Life Anchor is only ever created by an explicit user action (this API) or,
in Phase 2B, after an explicit in-chat confirmation. See
docs/MIRRORGPT_MEMORY_PLAN.md Phase 2.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core.security import get_current_user
from ..models.life_anchor import AnchorScopes, LifeAnchor
from ..repositories.life_anchor_repo import LifeAnchorRepo

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Life Anchors"])


# ============================================================
# Pydantic models
# ============================================================


AnchorType = Literal[
    "loss",
    "birth",
    "divorce",
    "diagnosis",
    "sobriety",
    "anniversary",
    "transition",
    "custom",
]
EmotionalWeight = Literal["sacred", "high", "medium"]
ReflectionUse = Literal["always_consider", "when_relevant", "never"]
AnchorStatus = Literal["active", "paused"]


class ScopesModel(BaseModel):
    mirrorgpt: bool = True
    echo_map: bool = False
    echo_vault: bool = False
    legacy_capsule: bool = False


class LifeAnchorCreateRequest(BaseModel):
    anchor_type: AnchorType = "custom"
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=1000)
    relationship: Optional[str] = Field(None, max_length=80)
    date: Optional[str] = Field(None, max_length=40)
    emotional_weight: EmotionalWeight = "medium"
    reflection_use: ReflectionUse = "when_relevant"
    scopes: Optional[ScopesModel] = None
    tone_guidance: List[str] = Field(default_factory=list)


class LifeAnchorUpdateRequest(BaseModel):
    """Partial update — omitted fields are left unchanged."""

    anchor_type: Optional[AnchorType] = None
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    relationship: Optional[str] = Field(None, max_length=80)
    date: Optional[str] = Field(None, max_length=40)
    emotional_weight: Optional[EmotionalWeight] = None
    reflection_use: Optional[ReflectionUse] = None
    status: Optional[AnchorStatus] = None
    scopes: Optional[ScopesModel] = None
    tone_guidance: Optional[List[str]] = None


class LifeAnchorOut(BaseModel):
    anchor_id: str
    anchor_type: str
    title: str
    description: str
    relationship: Optional[str] = None
    date: Optional[str] = None
    emotional_weight: str
    reflection_use: str
    status: str
    scopes: ScopesModel
    tone_guidance: List[str]
    created_from: str
    created_at: str
    updated_at: str


# ============================================================
# Dependency injection + helpers
# ============================================================


def get_life_anchor_repo() -> LifeAnchorRepo:
    return LifeAnchorRepo()


def _user_id_or_401(user: Dict[str, Any]) -> str:
    user_id = user.get("id") or user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="missing user id in claims")
    return user_id


def _to_out(anchor: LifeAnchor) -> LifeAnchorOut:
    return LifeAnchorOut(
        anchor_id=anchor.anchor_id,
        anchor_type=anchor.anchor_type,
        title=anchor.title,
        description=anchor.description,
        relationship=anchor.relationship,
        date=anchor.date,
        emotional_weight=anchor.emotional_weight,
        reflection_use=anchor.reflection_use,
        status=anchor.status,
        scopes=ScopesModel(
            mirrorgpt=anchor.scopes.mirrorgpt,
            echo_map=anchor.scopes.echo_map,
            echo_vault=anchor.scopes.echo_vault,
            legacy_capsule=anchor.scopes.legacy_capsule,
        ),
        tone_guidance=list(anchor.tone_guidance or []),
        created_from=anchor.created_from,
        created_at=anchor.created_at,
        updated_at=anchor.updated_at,
    )


async def _get_owned_or_404(
    repo: LifeAnchorRepo, user_id: str, anchor_id: str
) -> LifeAnchor:
    anchor = await repo.get(user_id, anchor_id)
    if anchor is None:
        raise HTTPException(status_code=404, detail="life anchor not found")
    return anchor


def _envelope(data: Any, message: str) -> Dict[str, Any]:
    return {"success": True, "data": data, "message": message}


# ============================================================
# GET /me/life-anchors
# ============================================================


@router.get(
    "/me/life-anchors",
    response_model=Dict[str, Any],
    summary="List the user's Life Anchors",
)
async def list_life_anchors(
    current_user: Dict[str, Any] = Depends(get_current_user),
    repo: LifeAnchorRepo = Depends(get_life_anchor_repo),
):
    user_id = _user_id_or_401(current_user)
    anchors = await repo.query_by_user(user_id)
    return _envelope([_to_out(a).model_dump() for a in anchors], "Life anchors loaded")


# ============================================================
# POST /me/life-anchors
# ============================================================


@router.post(
    "/me/life-anchors",
    response_model=Dict[str, Any],
    status_code=201,
    summary="Create a Life Anchor",
)
async def create_life_anchor(
    request: LifeAnchorCreateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    repo: LifeAnchorRepo = Depends(get_life_anchor_repo),
):
    user_id = _user_id_or_401(current_user)
    scopes = (
        AnchorScopes(**request.scopes.model_dump())
        if request.scopes is not None
        else AnchorScopes()
    )
    anchor = LifeAnchor(
        user_id=user_id,
        anchor_type=request.anchor_type,
        title=request.title,
        description=request.description,
        relationship=request.relationship,
        date=request.date,
        emotional_weight=request.emotional_weight,
        reflection_use=request.reflection_use,
        scopes=scopes,
        tone_guidance=list(request.tone_guidance or []),
        created_from="manual",
        user_confirmed=True,
    )
    saved = await repo.upsert(anchor)
    return _envelope(_to_out(saved).model_dump(), "Life anchor created")


# ============================================================
# PUT /me/life-anchors/{anchor_id}
# ============================================================


@router.put(
    "/me/life-anchors/{anchor_id}",
    response_model=Dict[str, Any],
    summary="Update a Life Anchor (partial)",
)
async def update_life_anchor(
    anchor_id: str,
    request: LifeAnchorUpdateRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    repo: LifeAnchorRepo = Depends(get_life_anchor_repo),
):
    user_id = _user_id_or_401(current_user)
    anchor = await _get_owned_or_404(repo, user_id, anchor_id)

    changes = request.model_dump(exclude_unset=True)
    for field_name in (
        "anchor_type",
        "title",
        "description",
        "relationship",
        "date",
        "emotional_weight",
        "reflection_use",
        "status",
        "tone_guidance",
    ):
        if field_name in changes and changes[field_name] is not None:
            setattr(anchor, field_name, changes[field_name])
    if changes.get("scopes") is not None:
        anchor.scopes = AnchorScopes(**changes["scopes"])
    anchor.touch()

    saved = await repo.upsert(anchor)
    return _envelope(_to_out(saved).model_dump(), "Life anchor updated")


# ============================================================
# POST /me/life-anchors/{anchor_id}/pause
# ============================================================


@router.post(
    "/me/life-anchors/{anchor_id}/pause",
    response_model=Dict[str, Any],
    summary="Pause a Life Anchor (stop using it in reflections)",
)
async def pause_life_anchor(
    anchor_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    repo: LifeAnchorRepo = Depends(get_life_anchor_repo),
):
    user_id = _user_id_or_401(current_user)
    anchor = await _get_owned_or_404(repo, user_id, anchor_id)
    anchor.status = "paused"
    anchor.touch()
    saved = await repo.upsert(anchor)
    return _envelope(_to_out(saved).model_dump(), "Life anchor paused")


# ============================================================
# DELETE /me/life-anchors/{anchor_id}
# ============================================================


@router.delete(
    "/me/life-anchors/{anchor_id}",
    response_model=Dict[str, Any],
    summary="Delete a Life Anchor",
)
async def delete_life_anchor(
    anchor_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
    repo: LifeAnchorRepo = Depends(get_life_anchor_repo),
):
    user_id = _user_id_or_401(current_user)
    await _get_owned_or_404(repo, user_id, anchor_id)
    await repo.delete(user_id, anchor_id)
    return _envelope(None, "Life anchor deleted")
