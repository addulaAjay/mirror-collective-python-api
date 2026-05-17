"""
Echo Vault API Routes.
Endpoints for managing Echoes, Recipients, and Guardians.
"""

import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, validator

from ..core.idempotency import idempotent
from ..core.security import get_current_user
from ..services.echo_service import get_echo_service
from ..services.email_service import email_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Echo Vault"])

# Initialize service
echo_service = get_echo_service()


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
    # Pydantic Literal validates at request time so malformed values get
    # a 422 before they reach the service — and prevents tag-string
    # injection in the presigned PUT's Tagging parameter.
    upload_type: Optional[Literal["echo", "profile", "user_profile"]] = "echo"


class FinalizeMediaRequest(BaseModel):
    """Client tells the backend: "I've finished PUT-ing to s3://.../{key}.
    Please verify it landed and atomically attach it to this echo."

    The backend will:
    - HEAD the object to confirm it exists.
    - Validate the key prefix matches the caller's namespace.
    - Persist the canonical (non-presigned) media_url to DDB.

    Closes the race between client PUT-success and client PATCH-commit,
    and removes the client's ability to forge a media_url server-side.
    """

    key: str  # S3 object key the client just uploaded to
    content_type: Optional[str] = None  # caller hint, overridden by S3 HEAD


class AttachPosterRequest(BaseModel):
    """Client tells the backend: "I've uploaded a poster frame for the
    video at echo {echo_id}; commit it as the thumbnail."

    The poster is a JPEG extracted client-side (via
    react-native-compressor.createVideoThumbnail) from the same video
    file the client just successfully uploaded. The backend verifies
    via HeadObject and writes ``poster_url`` to the echo row.
    """

    key: str  # S3 object key of the uploaded poster JPEG


class MultipartInitiateRequest(BaseModel):
    """Start an S3 multipart upload for a >50 MB file."""

    file_type: str  # MIME type — validated against the allowlist


class MultipartPartUrlsRequest(BaseModel):
    """Ask for presigned PUT URLs for a batch of part numbers.

    The client typically asks in batches of 4-8 (matching its upload
    concurrency) so the backend response stays small.
    """

    upload_id: str
    key: str
    # 1-indexed part numbers. S3 hard cap is 10,000; service-side
    # validation rejects out-of-range values. Service also caps batch size.
    part_numbers: List[int]


class CompletedPart(BaseModel):
    """An uploaded part as reported by the client.

    ETag comes from S3's per-part PUT response header. The service
    accepts both quoted (S3's wire format) and unquoted forms — the
    quoting is canonicalized before the CompleteMultipartUpload call.
    """

    part_number: int
    etag: str


class MultipartCompleteRequest(BaseModel):
    """Finalize the multipart upload + commit media_url to the echo row."""

    upload_id: str
    key: str
    parts: List[CompletedPart]


class MultipartAbortRequest(BaseModel):
    """Best-effort cancel of an in-progress multipart upload."""

    upload_id: str
    key: str


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
@idempotent(route_id="create_echo")
async def create_echo(
    payload: CreateEchoRequest,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Create a new echo in the vault.

    Idempotency: when the client sends an ``Idempotency-Key`` header,
    duplicate requests with the same key from the same user return the
    original response within 24 h. Lets clients safely retry after a
    network timeout without producing duplicate vault rows.
    """
    user_id = current_user["id"]

    echo = await echo_service.create_echo(user_id, payload.model_dump())

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
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Get presigned URL for direct media upload to S3."""
    from ..core.exceptions import ValidationError

    user_id = current_user["id"]

    try:
        result = await echo_service.generate_upload_url(
            user_id=user_id,
            file_type=request.file_type,
            echo_id=request.echo_id,
            upload_type=request.upload_type or "echo",
        )
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "success": True,
        "data": result,
        "message": "Upload URL generated",
    }


@router.post("/echoes/{echo_id}/finalize-media", response_model=Dict[str, Any])
@idempotent(route_id="finalize_media")
async def finalize_media_upload(
    echo_id: str,
    payload: FinalizeMediaRequest,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Verify a completed S3 PUT and atomically attach it to the echo.

    Replaces the old client-driven ``PATCH /echoes/:id`` with `media_url`
    payload pattern. The backend HEADs the S3 object server-side so the
    client cannot forge a URL, and the write is atomic — if HEAD fails
    we don't commit, so we never leave the echo row half-attached.

    Returns the full updated echo (same shape as ``GET /echoes/{id}``)
    so clients can refresh their cached state without a follow-up read.

    Idempotency: when the client sends an ``Idempotency-Key`` header,
    duplicate finalize calls (e.g. after a network blip mid-response)
    return the original response within 24 h.
    """
    from ..core.exceptions import NotFoundError, ValidationError

    user_id = current_user["id"]

    try:
        echo = await echo_service.finalize_upload(
            echo_id=echo_id,
            user_id=user_id,
            key=payload.key,
            content_type=payload.content_type,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

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
            "poster_url": echo.poster_url,
            "recipient_id": echo.recipient_id,
            "recipient": echo.recipient,
            "release_date": echo.release_date,
            "lock_date": echo.lock_date,
            "letter_to_recipient": echo.letter_to_recipient,
            "created_at": echo.created_at,
            "updated_at": echo.updated_at,
        },
        "message": "Echo media finalized",
    }


@router.post(
    "/echoes/{echo_id}/attach-poster",
    response_model=Dict[str, Any],
)
async def attach_poster_route(
    echo_id: str,
    payload: AttachPosterRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Attach a client-extracted poster frame to a video echo.

    The client uses ``react-native-compressor.createVideoThumbnail`` to
    extract a JPEG at t=1s during the save flow, uploads it via the
    standard single-PUT presigned URL, then calls this endpoint with
    the resulting S3 key. The backend HEADs the object to confirm and
    atomically writes ``poster_url`` to the echo row.

    Not @idempotent — poster attach is naturally idempotent (the
    second call just overwrites with the same URL).
    """
    from ..core.exceptions import NotFoundError, ValidationError

    user_id = current_user["id"]

    try:
        echo = await echo_service.attach_poster(
            echo_id=echo_id,
            user_id=user_id,
            key=payload.key,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "success": True,
        "data": {
            "echo_id": echo.echo_id,
            "poster_url": echo.poster_url,
        },
        "message": "Echo poster attached",
    }


# ========================================
# MULTIPART UPLOAD ROUTES (>50 MB files)
# ========================================
#
# Four-step ceremony around S3's MultipartUpload API. Compared to the
# single-PUT presign in /echoes/upload-url:
#
#   - resilient: a failed part retries individually instead of throwing
#     the whole upload away.
#   - parallel: the client uploads N parts at once, capped only by its
#     own concurrency setting (typically 4).
#
# The route surface mirrors S3's: initiate → part-urls → complete.
# abort is the cleanup path for client-side give-ups. The backend's
# bucket-lifecycle rule reaps abandoned uploads after 7 days as a
# defense-in-depth backstop.


@router.post("/echoes/{echo_id}/multipart/initiate", response_model=Dict[str, Any])
@idempotent(route_id="multipart_initiate")
async def initiate_multipart_upload(
    echo_id: str,
    payload: MultipartInitiateRequest,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Open a multipart upload session for this echo.

    Returns ``{upload_id, key, bucket}``. The client uses ``upload_id``
    on every subsequent multipart call.

    Idempotent: clients that retry through the BaseApiService 429 loop
    will get the same upload_id back rather than opening duplicate
    upload sessions (which would each tie up storage until reaped by
    the lifecycle rule).
    """
    from ..core.exceptions import NotFoundError, ValidationError

    user_id = current_user["id"]
    try:
        result = await echo_service.initiate_multipart_upload(
            echo_id=echo_id,
            user_id=user_id,
            file_type=payload.file_type,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "success": True,
        "data": result,
        "message": "Multipart upload initiated",
    }


@router.post("/echoes/{echo_id}/multipart/part-urls", response_model=Dict[str, Any])
async def get_multipart_part_urls(
    echo_id: str,
    payload: MultipartPartUrlsRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Generate presigned PUT URLs for a batch of part numbers.

    NOT decorated with @idempotent — these are URL-generation calls and
    caching them would hand back stale signatures after the original
    presign expired. Clients can safely call this again to refresh URLs.
    """
    from ..core.exceptions import NotFoundError, ValidationError

    user_id = current_user["id"]
    try:
        urls = await echo_service.generate_multipart_part_urls(
            echo_id=echo_id,
            user_id=user_id,
            upload_id=payload.upload_id,
            key=payload.key,
            part_numbers=payload.part_numbers,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "success": True,
        "data": {"part_urls": urls},
        "message": "Part URLs generated",
    }


@router.post("/echoes/{echo_id}/multipart/complete", response_model=Dict[str, Any])
@idempotent(route_id="multipart_complete")
async def complete_multipart_upload(
    echo_id: str,
    payload: MultipartCompleteRequest,
    request: Request,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Assemble the parts in S3 and commit media_url atomically.

    Returns the full updated echo (same shape as GET /echoes/{id}).

    Idempotent: a network blip between S3 complete and the response
    can leave the client retrying. Caching the response lets the
    retry return the same echo state without a second S3 complete
    (which would 404 — the upload session is already gone).
    """
    from ..core.exceptions import NotFoundError, ValidationError

    user_id = current_user["id"]
    parts_dicts = [
        {"part_number": p.part_number, "etag": p.etag} for p in payload.parts
    ]
    try:
        echo = await echo_service.complete_multipart_upload(
            echo_id=echo_id,
            user_id=user_id,
            upload_id=payload.upload_id,
            key=payload.key,
            parts=parts_dicts,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

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
            "poster_url": echo.poster_url,
            "recipient_id": echo.recipient_id,
            "recipient": echo.recipient,
            "release_date": echo.release_date,
            "lock_date": echo.lock_date,
            "letter_to_recipient": echo.letter_to_recipient,
            "created_at": echo.created_at,
            "updated_at": echo.updated_at,
        },
        "message": "Multipart upload completed",
    }


@router.post("/echoes/{echo_id}/multipart/abort", response_model=Dict[str, Any])
async def abort_multipart_upload(
    echo_id: str,
    payload: MultipartAbortRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Cancel an in-progress multipart upload.

    Idempotent by nature (NoSuchUpload is treated as success at the
    service layer), so no @idempotent decorator needed.
    """
    from ..core.exceptions import NotFoundError, ValidationError

    user_id = current_user["id"]
    try:
        await echo_service.abort_multipart_upload(
            echo_id=echo_id,
            user_id=user_id,
            upload_id=payload.upload_id,
            key=payload.key,
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"success": True, "message": "Multipart upload aborted"}


@router.get("/echoes", response_model=Dict[str, Any])
async def list_user_echoes(
    category: Optional[str] = Query(None, description="Filter by category"),
    recipient_id: Optional[str] = Query(None, description="Filter by recipient"),
    status: Optional[str] = Query(
        None, description="Filter by status (DRAFT, LOCKED, RELEASED)"
    ),
    limit: Optional[int] = Query(
        None, ge=1, le=100, description="Page size (1..100, default 50)"
    ),
    cursor: Optional[str] = Query(
        None, description="Opaque pagination cursor from a previous response"
    ),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """List echoes created by the current user (vault view), one page at a time.

    Pagination:
        - ``limit``: 1..100, default 50.
        - ``cursor``: pass the ``next_cursor`` from a prior response to fetch
          the next page. ``null`` when there are no more rows.
    """
    user_id = current_user["id"]

    echoes, next_cursor = await echo_service.get_user_echoes(
        user_id=user_id,
        category=category,
        recipient_id=recipient_id,
        status=status,
        limit=limit,
        cursor=cursor,
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
                # Pre-signed poster URL for video thumbnails in list
                # cards. Cheap to include — list-level sign is already
                # done in get_user_echoes via _sign_poster_urls_for_echoes.
                "poster_url": e.poster_url,
                "created_at": e.created_at,
            }
            for e in echoes
        ],
        "count": len(echoes),
        "next_cursor": next_cursor,
    }


@router.get("/echoes/inbox", response_model=Dict[str, Any])
async def list_received_echoes(
    category: Optional[str] = Query(None, description="Filter by category"),
    sender_id: Optional[str] = Query(None, description="Filter by sender"),
    limit: Optional[int] = Query(
        None, ge=1, le=100, description="Page size (1..100, default 50)"
    ),
    cursor: Optional[str] = Query(
        None, description="Opaque pagination cursor from a previous response"
    ),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """List echoes received by the current user (inbox view).

    Recipient match is by Cognito sub via recipients.recipient-user-id-index,
    which is populated at recipient creation and back-filled when the recipient
    later signs up. No email lookup is needed.

    Pagination:
        - ``limit``: 1..100, default 50.
        - ``cursor``: pass the ``next_cursor`` from a prior response to fetch
          the next page. ``null`` when there are no more rows.
    """
    user_id = current_user.get("id") or ""
    if not user_id:
        raise HTTPException(status_code=400, detail="User ID not found in token")

    try:
        echoes, next_cursor = await echo_service.get_received_echoes(
            user_id=user_id,
            category=category,
            sender_id=sender_id,
            limit=limit,
            cursor=cursor,
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
                # NOTE: media_url is deliberately omitted from the inbox list
                # response. The inbox card shows title/sender/category and
                # navigates to a playback screen that fetches the detail
                # endpoint (which signs media_url on demand). Eliminates the
                # N wasted presigns per page and closes the regression that
                # otherwise would have surfaced once the bucket public-access
                # block was tightened in the upload-Tier-A PR.
                "content": e.content,
                # Pre-signed poster URL for video thumbnails in inbox
                # cards. Same parallel-sign helper as the vault list.
                "poster_url": e.poster_url,
                "scheduled_at": e.release_date,
                "created_at": e.created_at,
            }
            for e in echoes
        ],
        "count": len(echoes),
        "next_cursor": next_cursor,
    }


@router.get("/echoes/{echo_id}", response_model=Dict[str, Any])
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
            "poster_url": echo.poster_url,
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
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Update an echo (only DRAFT status echoes can be updated)."""
    user_id = current_user["id"]

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
                "poster_url": echo.poster_url,
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


@router.patch("/echoes/{echo_id}/release", response_model=Dict[str, Any])
async def release_echo(
    echo_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
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

    user_id = current_user["id"]

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
    current_user: Dict[str, Any] = Depends(get_current_user),
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

    user_id = current_user["id"]

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
    limit: Optional[int] = Query(
        None, ge=1, le=100, description="Page size (1..100, default 50)"
    ),
    cursor: Optional[str] = Query(
        None, description="Opaque pagination cursor from a previous response"
    ),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """List recipients for the current user, one page at a time.

    Pagination:
        - ``limit``: 1..100, default 50.
        - ``cursor``: pass the ``next_cursor`` from a prior response to fetch
          the next page. ``null`` when there are no more rows.
    """
    user_id = current_user["id"]

    recipients, next_cursor = await echo_service.get_user_recipients(
        user_id, limit=limit, cursor=cursor
    )

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
        "next_cursor": next_cursor,
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
            "motif": recipient.motif,
            "profile_image_url": recipient.profile_image_url,
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
    limit: Optional[int] = Query(
        None, ge=1, le=100, description="Page size (1..100, default 50)"
    ),
    cursor: Optional[str] = Query(
        None, description="Opaque pagination cursor from a previous response"
    ),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """List guardians for the current user, one page at a time.

    Pagination:
        - ``limit``: 1..100, default 50.
        - ``cursor``: pass the ``next_cursor`` from a prior response to fetch
          the next page. ``null`` when there are no more rows.
    """
    user_id = current_user["id"]

    guardians, next_cursor = await echo_service.get_user_guardians(
        user_id, limit=limit, cursor=cursor
    )

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
        "next_cursor": next_cursor,
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
            "profile_image_url": guardian.profile_image_url,
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
