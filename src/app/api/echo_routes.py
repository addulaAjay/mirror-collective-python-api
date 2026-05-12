"""
Echo Vault API Routes.
Endpoints for managing Echoes, Recipients, and Guardians.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, validator

from ..core.entitlement import EntitledUser, require_entitled
from ..services.dynamodb_service import DynamoDBService
from ..services.echo_service import EchoService
from ..services.email_service import email_service
from ..services.storage_quota_service import StorageQuotaService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Echo Vault"])

# Initialize service
echo_service = EchoService()
_dynamodb_service = DynamoDBService()
quota_service = StorageQuotaService(_dynamodb_service)


# ========================================
# REQUEST/RESPONSE MODELS
# ========================================


class CreateEchoRequest(BaseModel):
    title: str
    category: str
    echo_type: str = "TEXT"  # TEXT, AUDIO, VIDEO
    recipient_id: Optional[str] = None
    guardian_id: Optional[str] = None
    release_date: Optional[str] = None  # ISO 8601 for scheduled release
    unlock_on_death: Optional[bool] = False  # If true, echo released when creator dies
    content: Optional[str] = None  # For text echoes
    # Optional cover note shown alongside the echo. Captured on the recipient
    # picker screen as "Letter to Recipient". Distinct from `content` so it
    # works for AUDIO / VIDEO echoes (where content is unused) as well.
    letter_to_recipient: Optional[str] = None

    @validator("release_date")
    def validate_release_date(cls, v):
        """Validate release_date is valid ISO 8601 format."""
        if v is not None:
            try:
                from datetime import datetime, timedelta, timezone

                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)

                # Prevent unreasonably old dates (> 1 year in past)
                if dt < now - timedelta(days=365):
                    raise ValueError(
                        "release_date cannot be more than 1 year in the past"
                    )

                # Prevent unreasonably far future dates (> 50 years)
                if dt > now + timedelta(days=365 * 50):
                    raise ValueError(
                        "release_date cannot be more than 50 years in the future"
                    )
            except (ValueError, AttributeError) as e:
                raise ValueError(
                    f"release_date must be valid ISO 8601 format: {str(e)}"
                )
        return v


class UpdateEchoRequest(BaseModel):
    """
    Patch payload for an existing echo. All fields are optional.

    Distinguishes "don't change" from "clear" by inspecting whether each field
    was explicitly set on the request (handled by `model_dump(exclude_unset=
    True)` in the route handler):

      - field omitted     → no change to the stored value
      - field set to null → clear (set stored value to None)
      - field set to val  → write `val`

    The service iterates with `if "<field>" in data` so a null value reaches
    the entity and clears it. See `EchoService.update_echo`.
    """

    title: Optional[str] = None
    category: Optional[str] = None
    content: Optional[str] = None
    media_url: Optional[str] = None
    recipient_id: Optional[str] = None
    release_date: Optional[str] = None  # ISO 8601; null clears the schedule
    letter_to_recipient: Optional[str] = None  # null clears the cover note

    @validator("release_date")
    def validate_release_date(cls, v):
        """Validate release_date is valid ISO 8601 format if provided."""
        if v is not None:
            try:
                from datetime import datetime, timedelta, timezone

                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)

                # Mirror CreateEchoRequest bounds — protect against typos.
                if dt < now - timedelta(days=365):
                    raise ValueError(
                        "release_date cannot be more than 1 year in the past"
                    )
                if dt > now + timedelta(days=365 * 50):
                    raise ValueError(
                        "release_date cannot be more than 50 years in the future"
                    )
            except (ValueError, AttributeError) as e:
                raise ValueError(
                    f"release_date must be valid ISO 8601 format: {str(e)}"
                )
        return v


class UploadUrlRequest(BaseModel):
    file_type: str  # MIME type: audio/mp4, video/mp4, image/jpeg
    echo_id: Optional[str] = None
    # 'echo' → echoes/{user_id}/ path
    # 'profile' → profiles/{user_id}/ path (recipient / guardian photo)
    # 'user_profile' → user_profiles/{user_id}/ path (own avatar)
    upload_type: Optional[str] = "echo"
    # Declared size in bytes of the file the client is about to upload.
    # Required for upload_type='echo' so the server can pre-flight the
    # storage quota check before issuing a presigned URL. Other upload
    # types ('profile', 'user_profile') aren't counted against the quota
    # so this field is optional for them.
    file_size_bytes: Optional[int] = None


class CreateRecipientRequest(BaseModel):
    name: str
    email: EmailStr
    relationship: Optional[str] = None
    motif: Optional[str] = None
    profile_image_url: Optional[str] = None  # S3 URL returned by upload-url flow


class CreateGuardianRequest(BaseModel):
    name: str
    email: EmailStr
    scope: str = "ALL"  # ALL, SELECTED
    trigger: str = "MANUAL"  # MANUAL, AUTOMATIC
    profile_image_url: Optional[str] = None  # S3 URL returned by upload-url flow


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
    recipient: Optional[Dict[str, Any]] = None
    created_at: str
    updated_at: str


class RecipientResponse(BaseModel):
    recipient_id: str
    user_id: str
    name: str
    email: str
    relationship: Optional[str] = None
    motif: Optional[str] = None
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


@router.post("/echoes", response_model=Dict[str, Any], status_code=201)
async def create_echo(
    request: CreateEchoRequest,
    entitled: EntitledUser = Depends(require_entitled),
):
    """Create a new echo in the vault."""
    user_id = entitled.user_id

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


@router.post("/echoes/upload-url", response_model=Dict[str, Any])
async def get_upload_url(
    request: UploadUrlRequest,
    entitled: EntitledUser = Depends(require_entitled),
):
    """Get presigned URL for direct media upload to S3.

    Pre-flights the user's storage quota for `upload_type='echo'` so we
    don't issue a signed URL the user can't legitimately fill. The check
    is skipped for non-vault upload types (profile photos, etc.) since
    those don't count against `echo_vault_used_gb`.
    """
    user_id = entitled.user_id
    upload_type = request.upload_type or "echo"

    if upload_type == "echo":
        quota_check = await quota_service.can_upload(
            user_id=user_id,
            file_size_bytes=request.file_size_bytes or 0,
        )
        if not quota_check.get("can_upload"):
            # 413 Payload Too Large is the closest semantic match for
            # "exceeds quota". `no_quota` shouldn't happen here because
            # require_entitled already gated the request, but treat it
            # as 402 if it does.
            reason = quota_check.get("reason") or "quota_exceeded"
            status_code = 402 if reason == "no_quota" else 413
            raise HTTPException(
                status_code=status_code,
                detail={
                    "code": reason,
                    "reason": reason,
                    "message": quota_check.get(
                        "message",
                        "Upload would exceed your storage quota.",
                    ),
                    "quota_status": quota_check.get("quota_status"),
                },
            )

    result = await echo_service.generate_upload_url(
        user_id=user_id,
        file_type=request.file_type,
        echo_id=request.echo_id,
        upload_type=upload_type,
    )

    return {
        "success": True,
        "data": result,
        "message": "Upload URL generated",
    }


@router.get("/echoes", response_model=Dict[str, Any])
async def list_user_echoes(
    category: Optional[str] = Query(None, description="Filter by category"),
    recipient_id: Optional[str] = Query(None, description="Filter by recipient"),
    status: Optional[str] = Query(
        None, description="Filter by status (DRAFT, LOCKED, RELEASED)"
    ),
    entitled: EntitledUser = Depends(require_entitled),
):
    """List all echoes created by the current user (vault view)."""
    user_id = entitled.user_id

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
                "recipient": e.recipient,
                "release_date": e.release_date,
                "lock_date": e.lock_date,
                "letter_to_recipient": e.letter_to_recipient,
                "created_at": e.created_at,
            }
            for e in echoes
        ],
        "count": len(echoes),
    }


@router.get("/echoes/inbox", response_model=Dict[str, Any])
async def list_received_echoes(
    category: Optional[str] = Query(None, description="Filter by category"),
    sender_id: Optional[str] = Query(None, description="Filter by sender"),
    entitled: EntitledUser = Depends(require_entitled),
):
    """List echoes received by the current user (inbox view).

    Recipient match is by Cognito sub via recipients.recipient-user-id-index,
    which is populated at recipient creation and back-filled when the recipient
    later signs up. No email lookup is needed.
    """
    user_id = entitled.user_id

    try:
        echoes = await echo_service.get_received_echoes(
            user_id=user_id,
            category=category,
            sender_id=sender_id,
        )
    except Exception:
        # Full traceback already logged inside the service. Surface a friendly
        # message to the client — never leak DynamoDB / boto error text.
        logger.exception(f"Inbox load failed for user {user_id}")
        raise HTTPException(
            status_code=503,
            detail="We couldn't load your inbox right now. Please try again.",
        )

    return {
        "success": True,
        "data": [
            {
                "echo_id": e.echo_id,
                "title": e.title,
                "category": e.category,
                "echo_type": e.echo_type.value,
                # sender object — matches EchoResponse.sender shape expected by the app.
                # name is not stored on the echo; the app falls back to user_id for now.
                "sender": {
                    "user_id": e.user_id,
                    "name": e.user_id,  # TODO: enrich with Cognito display name
                    "email": "",
                },
                "media_url": e.media_url,
                "content": e.content,
                "scheduled_at": e.release_date,
                "created_at": e.created_at,
            }
            for e in echoes
        ],
        "count": len(echoes),
    }


@router.get("/echoes/{echo_id}", response_model=Dict[str, Any])
async def get_echo(
    echo_id: str,
    entitled: EntitledUser = Depends(require_entitled),
):
    """Get a specific echo by ID."""
    user_id = entitled.user_id

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
            "recipient": echo.recipient,
            # release_date / lock_date / letter_to_recipient are needed by
            # the playback screens so they can prefill the edit flow with
            # the echo's current schedule + cover note.
            "release_date": echo.release_date,
            "lock_date": echo.lock_date,
            "letter_to_recipient": echo.letter_to_recipient,
            "created_at": echo.created_at,
            "updated_at": echo.updated_at,
        },
    }


@router.put("/echoes/{echo_id}", response_model=Dict[str, Any])
@router.patch("/echoes/{echo_id}", response_model=Dict[str, Any])
async def update_echo(
    echo_id: str,
    request: UpdateEchoRequest,
    entitled: EntitledUser = Depends(require_entitled),
):
    """Update an echo (only DRAFT status echoes can be updated)."""
    user_id = entitled.user_id

    try:
        # exclude_unset keeps explicit nulls (so callers can clear release_date
        # / recipient_id) while still ignoring fields the client didn't send.
        echo = await echo_service.update_echo(
            echo_id=echo_id,
            user_id=user_id,
            data=request.model_dump(exclude_unset=True),
        )

        # Return the full echo (same shape as GET /api/echoes/{id}) so the
        # caller can refresh its cached state directly from this response
        # without a follow-up fetch. In particular, the app needs `status`
        # to decide whether to fire the release endpoint after the PATCH.
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
                "recipient": echo.recipient,
                "release_date": echo.release_date,
                "lock_date": echo.lock_date,
                "letter_to_recipient": echo.letter_to_recipient,
                "created_at": echo.created_at,
                "updated_at": echo.updated_at,
            },
            "message": "Echo updated successfully",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/echoes/{echo_id}", response_model=Dict[str, Any])
async def delete_echo(
    echo_id: str,
    entitled: EntitledUser = Depends(require_entitled),
):
    """Soft delete an echo."""
    user_id = entitled.user_id

    success = await echo_service.delete_echo(echo_id, user_id)

    if not success:
        raise HTTPException(status_code=404, detail="Echo not found")

    # Recompute echo_vault_used_gb from the current S3 inventory. Soft-delete
    # alone does not free storage (the S3 object remains), but this keeps
    # the denormalised counter on UserProfile fresh for the next quota
    # check + paywall calculation, instead of waiting for the user's next
    # GET /api/subscriptions/quota-status. Failures are logged and ignored
    # — the delete itself has already succeeded.
    try:
        await quota_service.update_user_quota(user_id)
    except Exception as exc:  # noqa: BLE001 — best-effort recompute
        logger.warning(
            "Quota recompute after delete failed for user %s echo %s: %s",
            user_id,
            echo_id,
            exc,
        )

    return {
        "success": True,
        "message": "Echo deleted successfully",
    }


@router.patch("/echoes/{echo_id}/release", response_model=Dict[str, Any])
async def release_echo(
    echo_id: str,
    entitled: EntitledUser = Depends(require_entitled),
):
    """
    Release an echo directly to its recipient (no-guardian path).

    Preconditions (all checked by EchoService.release_echo):
    - Echo must be a DRAFT owned by the caller.
    - Echo must have a recipient_id.
    - Echo must NOT have a guardian_id (those echoes go via guardian flow).

    On success the echo status transitions to RELEASED and the recipient
    receives a notification email.
    """
    from ..core.exceptions import NotFoundError, ValidationError

    user_id = entitled.user_id

    try:
        echo = await echo_service.release_echo(echo_id=echo_id, user_id=user_id)

        return {
            "success": True,
            "data": {
                "echo_id": echo.echo_id,
                "status": echo.status.value,
                "updated_at": echo.updated_at,
            },
            "message": "Echo released successfully",
        }
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error releasing echo {echo_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to release echo")


@router.patch("/echoes/{echo_id}/lock", response_model=Dict[str, Any])
async def lock_echo(
    echo_id: str,
    entitled: EntitledUser = Depends(require_entitled),
):
    """
    Lock an echo with a guardian (Phase 2).

    Preconditions (all checked by EchoService.lock_echo):
    - Echo must be a DRAFT owned by the caller.
    - Echo must have a guardian_id.

    On success the echo status transitions to LOCKED, lock_date is set,
    and the guardian receives a notification email.
    """
    from ..core.exceptions import NotFoundError, ValidationError

    user_id = entitled.user_id

    try:
        echo = await echo_service.lock_echo(echo_id=echo_id, user_id=user_id)

        return {
            "success": True,
            "data": {
                "echo_id": echo.echo_id,
                "status": echo.status.value,
                "lock_date": echo.lock_date,
                "updated_at": echo.updated_at,
            },
            "message": "Echo locked successfully",
        }
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error locking echo {echo_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to lock echo")


# ========================================
# RECIPIENT ENDPOINTS
# ========================================


@router.get("/recipients", response_model=Dict[str, Any])
async def list_recipients(
    entitled: EntitledUser = Depends(require_entitled),
):
    """List all recipients for the current user."""
    user_id = entitled.user_id

    recipients = await echo_service.get_user_recipients(user_id)

    return {
        "success": True,
        "data": [
            {
                "recipient_id": r.recipient_id,
                "name": r.name,
                "email": r.email,
                "relationship": r.relationship,
                "motif": r.motif,
                "profile_image_url": r.profile_image_url,
                "created_at": r.created_at,
            }
            for r in recipients
        ],
        "count": len(recipients),
    }


@router.post("/recipients", response_model=Dict[str, Any], status_code=201)
async def create_recipient(
    request: CreateRecipientRequest,
    entitled: EntitledUser = Depends(require_entitled),
):
    """Add a new recipient. Triggers invitation email."""
    user_id = entitled.user_id
    user_name = entitled.user.get("name", entitled.user.get("email", "Someone"))

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
            "motif": recipient.motif,
            "profile_image_url": recipient.profile_image_url,
        },
        "message": "Recipient added successfully",
    }


@router.delete("/recipients/{recipient_id}", response_model=Dict[str, Any])
async def delete_recipient(
    recipient_id: str,
    entitled: EntitledUser = Depends(require_entitled),
):
    """Soft delete a recipient."""
    user_id = entitled.user_id

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
    entitled: EntitledUser = Depends(require_entitled),
):
    """List all guardians for the current user."""
    user_id = entitled.user_id

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
                "profile_image_url": g.profile_image_url,
                "created_at": g.created_at,
            }
            for g in guardians
        ],
        "count": len(guardians),
    }


@router.post("/guardians", response_model=Dict[str, Any], status_code=201)
async def create_guardian(
    request: CreateGuardianRequest,
    entitled: EntitledUser = Depends(require_entitled),
):
    """Add a new guardian. Triggers invitation email."""
    user_id = entitled.user_id
    user_name = entitled.user.get("name", entitled.user.get("email", "Someone"))

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
            "profile_image_url": guardian.profile_image_url,
        },
        "message": "Guardian added successfully",
    }


@router.put("/guardians/{guardian_id}/permissions", response_model=Dict[str, Any])
async def update_guardian_permissions(
    guardian_id: str,
    request: UpdateGuardianPermissionsRequest,
    entitled: EntitledUser = Depends(require_entitled),
):
    """Update guardian access permissions."""
    user_id = entitled.user_id

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
    entitled: EntitledUser = Depends(require_entitled),
):
    """Soft delete a guardian."""
    user_id = entitled.user_id

    success = await echo_service.delete_guardian(guardian_id, user_id)

    if not success:
        raise HTTPException(status_code=404, detail="Guardian not found")

    return {
        "success": True,
        "message": "Guardian deleted successfully",
    }
