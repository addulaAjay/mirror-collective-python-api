"""
Echo Vault API Routes.
Endpoints for managing Echoes, Recipients, and Guardians.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr

from ..core.security import get_current_user
from ..services.echo_service import EchoService
from ..services.email_service import email_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/echoes", tags=["Echo Vault"])

# Initialize service
echo_service = EchoService()


# ========================================
# REQUEST/RESPONSE MODELS
# ========================================


class CreateEchoRequest(BaseModel):
    title: str
    category: str
    echo_type: str = "TEXT"  # TEXT, AUDIO, VIDEO
    recipient_id: Optional[str] = None
    content: Optional[str] = None  # For text echoes


class UpdateEchoRequest(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    content: Optional[str] = None
    media_url: Optional[str] = None
    recipient_id: Optional[str] = None


class UploadUrlRequest(BaseModel):
    file_type: str  # MIME type: audio/mp4, video/mp4
    echo_id: Optional[str] = None


class CreateRecipientRequest(BaseModel):
    name: str
    email: EmailStr
    relationship: Optional[str] = None


class CreateGuardianRequest(BaseModel):
    name: str
    email: EmailStr
    scope: str = "ALL"  # ALL, SELECTED
    trigger: str = "MANUAL"  # MANUAL, AUTOMATIC


class UpdateGuardianPermissionsRequest(BaseModel):
    scope: Optional[str] = None
    trigger: Optional[str] = None
    allowed_echo_ids: Optional[List[str]] = None
    allowed_recipient_ids: Optional[List[str]] = None


class EchoResponse(BaseModel):
    echo_id: str
    user_id: str
    title: str
    category: str
    echo_type: str
    status: str
    media_url: Optional[str] = None
    content: Optional[str] = None
    recipient_id: Optional[str] = None
    created_at: str
    updated_at: str


class RecipientResponse(BaseModel):
    recipient_id: str
    user_id: str
    name: str
    email: str
    relationship: Optional[str] = None
    created_at: str


class GuardianResponse(BaseModel):
    guardian_id: str
    user_id: str
    name: str
    email: str
    scope: str
    trigger: str
    created_at: str


# ========================================
# ECHO ENDPOINTS
# ========================================


@router.post("", response_model=Dict[str, Any], status_code=201)
async def create_echo(
    request: CreateEchoRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Create a new echo in the vault."""
    user_id = current_user["id"]

    echo = await echo_service.create_echo(user_id, request.model_dump())

    return {
        "success": True,
        "data": {
            "echo_id": echo.echo_id,
            "title": echo.title,
            "category": echo.category,
            "echo_type": echo.echo_type.value,
            "status": echo.status.value,
        },
        "message": "Echo created successfully",
    }


@router.post("/upload-url", response_model=Dict[str, Any])
async def get_upload_url(
    request: UploadUrlRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Get presigned URL for direct media upload to S3."""
    user_id = current_user["id"]

    result = await echo_service.generate_upload_url(
        user_id=user_id,
        file_type=request.file_type,
        echo_id=request.echo_id,
    )

    return {
        "success": True,
        "data": result,
        "message": "Upload URL generated",
    }


@router.get("", response_model=Dict[str, Any])
async def list_user_echoes(
    category: Optional[str] = Query(None, description="Filter by category"),
    recipient_id: Optional[str] = Query(None, description="Filter by recipient"),
    status: Optional[str] = Query(
        None, description="Filter by status (DRAFT, LOCKED, RELEASED)"
    ),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """List all echoes created by the current user (vault view)."""
    user_id = current_user["id"]

    echoes = await echo_service.get_user_echoes(
        user_id=user_id,
        category=category,
        recipient_id=recipient_id,
        status=status,
    )

    return {
        "success": True,
        "data": [
            {
                "echo_id": e.echo_id,
                "title": e.title,
                "category": e.category,
                "echo_type": e.echo_type.value,
                "status": e.status.value,
                "recipient_id": e.recipient_id,
                "created_at": e.created_at,
            }
            for e in echoes
        ],
        "count": len(echoes),
    }


@router.get("/inbox", response_model=Dict[str, Any])
async def list_received_echoes(
    category: Optional[str] = Query(None, description="Filter by category"),
    sender_id: Optional[str] = Query(None, description="Filter by sender"),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """List echoes received by the current user (inbox view)."""
    user_email = current_user.get("email", "")

    if not user_email:
        raise HTTPException(status_code=400, detail="User email not found")

    echoes = await echo_service.get_received_echoes(
        recipient_email=user_email,
        category=category,
        sender_id=sender_id,
    )

    return {
        "success": True,
        "data": [
            {
                "echo_id": e.echo_id,
                "title": e.title,
                "category": e.category,
                "echo_type": e.echo_type.value,
                "sender_id": e.user_id,
                "created_at": e.created_at,
            }
            for e in echoes
        ],
        "count": len(echoes),
    }


@router.get("/{echo_id}", response_model=Dict[str, Any])
async def get_echo(
    echo_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Get a specific echo by ID."""
    user_id = current_user["id"]

    echo = await echo_service.get_echo(echo_id, user_id)

    if not echo:
        raise HTTPException(status_code=404, detail="Echo not found")

    return {
        "success": True,
        "data": {
            "echo_id": echo.echo_id,
            "title": echo.title,
            "category": echo.category,
            "echo_type": echo.echo_type.value,
            "status": echo.status.value,
            "content": echo.content,
            "media_url": echo.media_url,
            "recipient_id": echo.recipient_id,
            "created_at": echo.created_at,
            "updated_at": echo.updated_at,
        },
    }


@router.put("/{echo_id}", response_model=Dict[str, Any])
@router.patch("/{echo_id}", response_model=Dict[str, Any])
async def update_echo(
    echo_id: str,
    request: UpdateEchoRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Update an echo (only DRAFT status echoes can be updated)."""
    user_id = current_user["id"]

    try:
        echo = await echo_service.update_echo(
            echo_id=echo_id,
            user_id=user_id,
            data=request.model_dump(exclude_none=True),
        )

        return {
            "success": True,
            "data": {
                "echo_id": echo.echo_id,
                "title": echo.title,
                "updated_at": echo.updated_at,
            },
            "message": "Echo updated successfully",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{echo_id}", response_model=Dict[str, Any])
async def delete_echo(
    echo_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Soft delete an echo."""
    user_id = current_user["id"]

    success = await echo_service.delete_echo(echo_id, user_id)

    if not success:
        raise HTTPException(status_code=404, detail="Echo not found")

    return {
        "success": True,
        "message": "Echo deleted successfully",
    }


# ========================================
# RECIPIENT ENDPOINTS
# ========================================


@router.get("/recipients", response_model=Dict[str, Any])
async def list_recipients(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """List all recipients for the current user."""
    user_id = current_user["id"]

    recipients = await echo_service.get_user_recipients(user_id)

    return {
        "success": True,
        "data": [
            {
                "recipient_id": r.recipient_id,
                "name": r.name,
                "email": r.email,
                "relationship": r.relationship,
                "created_at": r.created_at,
            }
            for r in recipients
        ],
        "count": len(recipients),
    }


@router.post("/recipients", response_model=Dict[str, Any], status_code=201)
async def create_recipient(
    request: CreateRecipientRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Add a new recipient. Triggers invitation email."""
    user_id = current_user["id"]
    user_name = current_user.get("name", current_user.get("email", "Someone"))

    recipient = await echo_service.create_recipient(user_id, request.model_dump())

    # Send invitation email (fire-and-forget, don't block on failure)
    try:
        await email_service.send_recipient_invite(
            recipient_email=recipient.email,
            recipient_name=recipient.name,
            inviter_name=user_name,
        )
    except Exception as e:
        logger.warning(f"Failed to send recipient invite email: {e}")

    return {
        "success": True,
        "data": {
            "recipient_id": recipient.recipient_id,
            "name": recipient.name,
            "email": recipient.email,
        },
        "message": "Recipient added successfully",
    }


@router.delete("/recipients/{recipient_id}", response_model=Dict[str, Any])
async def delete_recipient(
    recipient_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Soft delete a recipient."""
    user_id = current_user["id"]

    success = await echo_service.delete_recipient(recipient_id, user_id)

    if not success:
        raise HTTPException(status_code=404, detail="Recipient not found")

    return {
        "success": True,
        "message": "Recipient deleted successfully",
    }


# ========================================
# GUARDIAN ENDPOINTS
# ========================================


@router.get("/guardians", response_model=Dict[str, Any])
async def list_guardians(
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """List all guardians for the current user."""
    user_id = current_user["id"]

    guardians = await echo_service.get_user_guardians(user_id)

    return {
        "success": True,
        "data": [
            {
                "guardian_id": g.guardian_id,
                "name": g.name,
                "email": g.email,
                "scope": g.scope.value,
                "trigger": g.trigger.value,
                "created_at": g.created_at,
            }
            for g in guardians
        ],
        "count": len(guardians),
    }


@router.post("/guardians", response_model=Dict[str, Any], status_code=201)
async def create_guardian(
    request: CreateGuardianRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Add a new guardian. Triggers invitation email."""
    user_id = current_user["id"]
    user_name = current_user.get("name", current_user.get("email", "Someone"))

    guardian = await echo_service.create_guardian(user_id, request.model_dump())

    # Send invitation email (fire-and-forget, don't block on failure)
    try:
        await email_service.send_guardian_invite(
            guardian_email=guardian.email,
            guardian_name=guardian.name,
            inviter_name=user_name,
            scope=guardian.scope.value,
        )
    except Exception as e:
        logger.warning(f"Failed to send guardian invite email: {e}")

    return {
        "success": True,
        "data": {
            "guardian_id": guardian.guardian_id,
            "name": guardian.name,
            "email": guardian.email,
            "scope": guardian.scope.value,
            "trigger": guardian.trigger.value,
        },
        "message": "Guardian added successfully",
    }


@router.put("/guardians/{guardian_id}/permissions", response_model=Dict[str, Any])
async def update_guardian_permissions(
    guardian_id: str,
    request: UpdateGuardianPermissionsRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Update guardian access permissions."""
    user_id = current_user["id"]

    try:
        guardian = await echo_service.update_guardian_permissions(
            guardian_id=guardian_id,
            user_id=user_id,
            data=request.model_dump(exclude_none=True),
        )

        return {
            "success": True,
            "data": {
                "guardian_id": guardian.guardian_id,
                "scope": guardian.scope.value,
                "trigger": guardian.trigger.value,
            },
            "message": "Guardian permissions updated",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/guardians/{guardian_id}", response_model=Dict[str, Any])
async def delete_guardian(
    guardian_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Soft delete a guardian."""
    user_id = current_user["id"]

    success = await echo_service.delete_guardian(guardian_id, user_id)

    if not success:
        raise HTTPException(status_code=404, detail="Guardian not found")

    return {
        "success": True,
        "message": "Guardian deleted successfully",
    }
