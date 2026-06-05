"""
Echo Service for Echo Vault feature.
Handles CRUD operations for Echoes, Recipients, and Guardians.
Includes S3 presigned URL generation for media uploads.
"""

import asyncio
import base64
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from ..core.exceptions import InternalServerError, NotFoundError, ValidationError
from ..core.share_token import build_share_url, create_share_token
from ..models.echo import (
    Attachment,
    AttachmentType,
    Echo,
    EchoStatus,
    EchoType,
    Guardian,
    GuardianScope,
    GuardianTrigger,
    Recipient,
)
from .email_service import email_service

logger = logging.getLogger(__name__)


# MIME types we'll generate presigned PUT URLs for. The S3 PUT will reject
# anything else via Content-Type mismatch, but rejecting at the API layer
# also keeps malformed Content-Type strings from leaking into our object
# keyspace and stops casual enumeration of bucket layout.
ALLOWED_UPLOAD_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
        "audio/m4a",
        "audio/mp4",
        "audio/mpeg",
        "audio/aac",
        "video/mp4",
        "video/quicktime",
        "video/x-m4v",
        # Document attachments — the "File" option in the Create-an-Echo
        # upload sheet (Figma 7544:2839 allows .pdf alongside .png/.jpg/.mp4).
        "application/pdf",
    }
)

# Canonical file extension per upload MIME type. Single source of truth for the
# single-PUT and multipart key builders. Video falls back to mp4 (camera-roll
# exports); anything off the allowlist never reaches here (rejected earlier).
_UPLOAD_EXT_BY_MIME: dict[str, str] = {
    "audio/m4a": "m4a",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/mpeg": "mp3",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "application/pdf": "pdf",
}


def _upload_extension_for(file_type: str) -> str:
    """Map an upload MIME type to its stored file extension."""
    if file_type in _UPLOAD_EXT_BY_MIME:
        return _UPLOAD_EXT_BY_MIME[file_type]
    if "video" in file_type:
        return "mp4"
    return "bin"


# Common non-canonical MIME types clients send (image pickers, older Androids)
# normalized to the canonical type the allowlist accepts. image/jpg is the big
# one — gallery pickers report it but it isn't a registered MIME type.
_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "audio/x-m4a": "audio/m4a",
    "audio/x-aac": "audio/aac",
    "video/mov": "video/quicktime",
    "video/x-quicktime": "video/quicktime",
}


def _normalize_mime(file_type: Optional[str]) -> str:
    """Lowercase + map known aliases (e.g. image/jpg -> image/jpeg)."""
    ct = (file_type or "").strip().lower()
    return _MIME_ALIASES.get(ct, ct)


# Pattern that flags a presigned S3 URL. We refuse to write any of these
# query-string parameters back into DynamoDB via update_echo, because
# presigned URLs are short-lived and would corrupt the canonical media_url.
# See PR description for the bug this closes.
_PRESIGNED_URL_MARKERS = re.compile(
    r"[?&](?:X-Amz-(?:Signature|Algorithm|Credential|Date|Expires|SignedHeaders)|"
    r"AWSAccessKeyId|Signature)=",
    re.IGNORECASE,
)


def _looks_like_presigned_url(url: Optional[str]) -> bool:
    """True if the URL carries SigV2/SigV4 presign query-string markers."""
    if not url:
        return False
    return bool(_PRESIGNED_URL_MARKERS.search(url))


def _short(s: str, n: int = 12) -> str:
    """Truncate a long identifier for log lines without assuming length.

    S3 upload IDs are typically ~100+ chars but a test double or future
    code path could return something shorter; ``s[:12] + "..."`` on a
    short string is misleading. This helper returns the input unchanged
    when it's already short enough.
    """
    if len(s) <= n:
        return s
    return f"{s[:n]}..."


# Cache directives we stamp on every PUT presign. Echo media keys embed a
# timestamp + echo_id so the bytes are effectively immutable; the long
# max-age + immutable lets browsers and CloudFront (Tier 3) cache without
# revalidation. Tagging carries cost-allocation + lifecycle hints.
_PUT_CACHE_CONTROL = "public, max-age=31536000, immutable"


# Pagination defaults. Kept conservative so a single bad client can't fan
# out a 10k-row response through one call; cursor-based paging is the
# escape hatch for inboxes / vaults with more rows than fit one page.
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100

# DynamoDB hard cap on BatchGetItem (per request, before throttling).
BATCH_GET_ITEM_MAX = 100


def _current_timestamp() -> str:
    """Get current UTC timestamp in ISO format"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clamp_limit(limit: Optional[int]) -> int:
    """Normalize an optional limit to within [1, MAX_PAGE_LIMIT]."""
    if limit is None or limit <= 0:
        return DEFAULT_PAGE_LIMIT
    return min(limit, MAX_PAGE_LIMIT)


def encode_cursor(last_evaluated_key: Optional[Dict[str, Any]]) -> Optional[str]:
    """Encode a DynamoDB LastEvaluatedKey into an opaque, URL-safe cursor.

    Returns None when there is no further page (the natural end-of-query
    signal we surface to API clients as ``next_cursor: null``).

    TODO(security-hardening): the cursor is opaque (base64-encoded JSON) but
    not authenticated. A tampered cursor is harmless today because the
    Query's KeyConditionExpression still pins the partition to the
    requesting user — DynamoDB rejects any ExclusiveStartKey whose hash
    doesn't match. But defense-in-depth would HMAC the cursor with a
    deploy-secret. Follow-up PR.
    """
    if not last_evaluated_key:
        return None
    try:
        raw = json.dumps(last_evaluated_key, default=str).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")
    except (TypeError, ValueError) as e:
        # A non-serializable LastEvaluatedKey is an upstream bug; refusing
        # to encode is safer than handing back a corrupt cursor.
        logger.error(f"Failed to encode pagination cursor: {e}")
        return None


def decode_cursor(cursor: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode a client-supplied cursor back into a DynamoDB ExclusiveStartKey.

    Bad / tampered cursors return ``None`` so the caller transparently
    restarts at page 1 — the client may have rolled across a deploy that
    changed the key shape.
    """
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            logger.warning("Decoded cursor is not a dict; ignoring")
            return None
        return decoded
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        logger.warning(f"Ignoring malformed pagination cursor: {e}")
        return None


class EchoService:
    """
    Service for managing Echo Vault entities in DynamoDB.
    Also handles S3 presigned URL generation for media uploads.

    aioboto3 clients/resources are built lazily on first use and reused for
    the lifetime of the service (typically the lifetime of the Lambda
    container). Re-entering a fresh aioboto3 session+resource on every
    method call costs ~50-150 ms of socket setup + SigV4 init, which on a
    warm Lambda is the single largest tail-latency contributor for echo
    routes. See sibling refactor in ``dynamodb_service.py``.
    """

    def __init__(self) -> None:
        """Initialize Echo service"""
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.echoes_table = os.getenv("DYNAMODB_ECHOES_TABLE", "echoes")
        self.recipients_table = os.getenv(
            "DYNAMODB_RECIPIENTS_TABLE", "echo_recipients"
        )
        self.guardians_table = os.getenv("DYNAMODB_GUARDIANS_TABLE", "echo_guardians")
        self.endpoint_url = os.getenv("DYNAMODB_ENDPOINT_URL")  # For local DynamoDB

        # S3 configuration
        self.s3_bucket = os.getenv("ECHO_MEDIA_BUCKET", "echo-vault-media")
        self.presigned_url_expiry = int(
            os.getenv("PRESIGNED_URL_EXPIRY", "3600")
        )  # 1 hour

        # Initialize aioboto3 session
        self.session = aioboto3.Session()

        # Long-lived resource/client caches. Populated on first use via
        # ``_get_dynamodb_resource`` / ``_get_s3_client``. The AsyncExitStack
        # owns the entered context managers so they survive across requests
        # within the same container.
        self._exit_stack: Optional[AsyncExitStack] = None
        self._dynamodb_resource: Any = None
        self._s3_client: Any = None
        self._init_lock: Optional[asyncio.Lock] = None

        # Tuned connection pool — Lambda containers may handle a burst of
        # concurrent requests via the underlying httpcore pool. Adaptive
        # retries back off on throttling without exploding tail latency.
        self._boto_config = Config(
            max_pool_connections=50,
            retries={"max_attempts": 5, "mode": "adaptive"},
        )

        # Initialize DynamoDB service for user lookups
        from src.app.services.dynamodb_service import get_dynamodb_service

        self.dynamodb_service = get_dynamodb_service()

        target = "Local DynamoDB" if self.endpoint_url else "AWS DynamoDB"
        logger.info(
            f"EchoService initialized - Target: {target}, "
            f"Echoes Table: {self.echoes_table}, S3 Bucket: {self.s3_bucket}"
        )

    def _get_dynamodb_kwargs(self) -> Dict[str, Any]:
        """Get DynamoDB connection parameters (local or AWS)"""
        kwargs: Dict[str, Any] = {
            "region_name": self.region,
            "config": self._boto_config,
        }

        if self.endpoint_url:
            kwargs.update(
                {
                    "endpoint_url": self.endpoint_url,
                    "aws_access_key_id": "dummy",
                    "aws_secret_access_key": "dummy",
                }
            )

        return kwargs

    def _get_s3_kwargs(self) -> Dict[str, Any]:
        """Get S3 connection parameters (region + tuned client config).

        When ``S3_ACCELERATE_ENABLED`` is truthy in the environment we wire
        the botocore client to use the ``s3-accelerate`` endpoint, which
        routes uploads through the nearest CloudFront edge. The bucket
        must also have ``AccelerateConfiguration: Enabled`` (set in
        serverless.yml) for this to take effect; the client config alone
        is harmless but a no-op against a non-accelerated bucket.
        """
        use_accelerate = os.getenv("S3_ACCELERATE_ENABLED", "false").lower() in (
            "true",
            "1",
            "yes",
        )
        accel_config = self._boto_config
        if use_accelerate:
            accel_config = self._boto_config.merge(
                Config(s3={"use_accelerate_endpoint": True})
            )
        return {
            "region_name": self.region,
            "config": accel_config,
        }

    def _get_init_lock(self) -> asyncio.Lock:
        """Lazily allocate an asyncio.Lock bound to the current event loop.

        We cannot allocate the lock in __init__ because that runs at module
        import time, before an event loop exists for the Lambda invocation.

        TODO(correctness): there's a tiny race window where two simultaneous
        first-callers each see ``self._init_lock is None`` and each create
        their own Lock. Worst case is two distinct locks briefly + a single
        duplicate resource allocation. Lambda one-request-per-container
        means this is effectively unreachable, but technically the
        double-check inside ``_get_dynamodb_resource`` doesn't protect
        against this. Cleaner fix: switch to ``threading.Lock`` in
        ``__init__`` (cheap, no event loop needed). Follow-up PR.
        """
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        return self._init_lock

    async def _get_dynamodb_resource(self) -> Any:
        """Return the long-lived aioboto3 DynamoDB resource, entering it
        through the instance AsyncExitStack on first call.

        Subsequent calls return the cached resource directly — no socket
        setup, no SigV4 init, no per-call ``async with`` overhead.
        """
        if self._dynamodb_resource is not None:
            return self._dynamodb_resource

        async with self._get_init_lock():
            if self._dynamodb_resource is not None:
                return self._dynamodb_resource
            if self._exit_stack is None:
                self._exit_stack = AsyncExitStack()
                await self._exit_stack.__aenter__()
            resource_cm = self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            )
            self._dynamodb_resource = await self._exit_stack.enter_async_context(
                resource_cm
            )
            return self._dynamodb_resource

    async def _get_s3_client(self) -> Any:
        """Return the long-lived aioboto3 S3 client.

        Same lifecycle as the DynamoDB resource — built once, reused for
        the lifetime of the container.
        """
        if self._s3_client is not None:
            return self._s3_client

        async with self._get_init_lock():
            if self._s3_client is not None:
                return self._s3_client
            if self._exit_stack is None:
                self._exit_stack = AsyncExitStack()
                await self._exit_stack.__aenter__()
            client_cm = self.session.client("s3", **self._get_s3_kwargs())
            self._s3_client = await self._exit_stack.enter_async_context(client_cm)
            return self._s3_client

    async def aclose(self) -> None:
        """Tear down the long-lived clients. Used in tests; production
        Lambda containers do not call this — the kernel reaps sockets on
        container shutdown.
        """
        if self._exit_stack is not None:
            await self._exit_stack.__aexit__(None, None, None)
        self._exit_stack = None
        self._dynamodb_resource = None
        self._s3_client = None

    # ========================================
    # ECHO CRUD OPERATIONS
    # ========================================

    async def create_echo(self, user_id: str, data: Dict[str, Any]) -> Echo:
        """
        Create a new echo in the vault.

        Auto-release logic:
        - If has recipient_id, no guardian_id, and no release_date → release immediately
        - If has recipient_id, no guardian_id, and release_date in past → release immediately
        - If has recipient_id, no guardian_id, and release_date in future → save as DRAFT (needs scheduler)
        - If has guardian_id → always save as DRAFT (guardian workflow)

        Args:
            user_id: Owner's user ID
            data: Echo data (title, category, echo_type, release_date, etc.)

        Returns:
            Created Echo (potentially auto-released)
        """
        try:
            # Build Echo from data
            echo = Echo(
                user_id=user_id,
                title=data.get("title", ""),
                category=data.get("category", ""),
                echo_type=EchoType(data.get("echo_type", "TEXT")),
                recipient_id=data.get("recipient_id"),
                guardian_id=data.get("guardian_id"),
                release_date=data.get("release_date"),
                unlock_on_death=data.get("unlock_on_death", False),
                content=data.get("content"),  # For text type
                letter_to_recipient=data.get("letter_to_recipient"),
            )

            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)
            await table.put_item(Item=echo.to_dynamodb_item())

            logger.info(f"Created echo {echo.echo_id} for user {user_id}")

            # Auto-release: Check if immediate release is needed
            should_release_now = False
            if echo.recipient_id and not echo.guardian_id:
                if not echo.release_date:
                    # No scheduled date → release immediately
                    should_release_now = True
                    logger.info(
                        f"No release_date specified for echo {echo.echo_id}, releasing immediately"
                    )
                else:
                    # Check if release_date has passed
                    release_time = datetime.fromisoformat(
                        echo.release_date.replace("Z", "+00:00")
                    )
                    now = datetime.now(timezone.utc)
                    if release_time <= now:
                        should_release_now = True
                        logger.info(
                            f"Release date {echo.release_date} has passed for echo {echo.echo_id}, releasing now"
                        )
                    else:
                        logger.info(
                            f"Echo {echo.echo_id} scheduled for future release at {echo.release_date}"
                        )

            if should_release_now:
                logger.info(
                    f"Auto-releasing echo {echo.echo_id} to recipient {echo.recipient_id}"
                )
                echo.release()

                # Update status in DynamoDB
                dynamodb = await self._get_dynamodb_resource()
                table = await dynamodb.Table(self.echoes_table)
                await table.update_item(
                    Key={"echo_id": echo.echo_id},
                    UpdateExpression="SET #status = :status, updated_at = :updated_at",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":status": echo.status.value,
                        ":updated_at": echo.updated_at,
                    },
                )

                # Get recipient details for notification
                if echo.recipient_id:
                    recipient = await self.get_recipient(echo.recipient_id, user_id)
                    if recipient:
                        # Fire-and-forget notification
                        try:
                            # Check if recipient is registered (has recipient_user_id)
                            is_registered = recipient.recipient_user_id is not None

                            media_fields = await self.build_email_media_fields(
                                echo, recipient.recipient_id
                            )
                            await email_service.send_echo_notification(
                                recipient_email=recipient.email,
                                recipient_name=recipient.name,
                                sender_name=user_id,  # TODO: fetch actual user name
                                echo_title=echo.title,
                                echo_category=echo.category,
                                echo_type=echo.echo_type.value,
                                is_registered=is_registered,
                                quote=echo.letter_to_recipient or echo.content,
                                echo_date=echo.release_date or echo.updated_at,
                                **media_fields,
                            )
                            logger.info(
                                f"Sent auto-release notification for echo {echo.echo_id} (registered={is_registered})"
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to send auto-release notification: {e}",
                                exc_info=True,
                            )
                    else:
                        logger.warning(
                            f"Recipient {echo.recipient_id} not found for auto-release of echo {echo.echo_id}"
                        )

            return echo

        except ClientError as e:
            logger.error(f"DynamoDB error creating echo: {e}")
            raise InternalServerError(f"Failed to create echo: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error creating echo: {e}")
            raise InternalServerError(f"Unexpected error: {str(e)}")

    async def get_echo(self, echo_id: str, user_id: str) -> Optional[Echo]:
        """
        Get an echo by ID.

        Args:
            echo_id: Echo ID
            user_id: User ID (for authorization)

        Returns:
            Echo if found and owned by user, None otherwise
        """
        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)
            response = await table.get_item(Key={"echo_id": echo_id})

            if "Item" not in response:
                return None

            echo = Echo.from_dynamodb_item(response["Item"])

            # Security: Verify access - user must be either owner OR recipient
            is_owner = echo.user_id == user_id
            is_recipient = False
            recipient_from_access_check = None

            if is_owner:
                logger.info(f"User {user_id} accessing echo {echo_id} as owner")

            # Check if user is the recipient
            if not is_owner and echo.recipient_id:
                # get_recipient verifies that recipient belongs to echo.user_id (the echo owner)
                recipient = await self.get_recipient(echo.recipient_id, echo.user_id)
                if recipient and recipient.recipient_user_id == user_id:
                    is_recipient = True
                    recipient_from_access_check = (
                        recipient  # Save to avoid duplicate query
                    )
                    logger.info(
                        f"User {user_id} accessing echo {echo_id} as recipient (recipient_id: {echo.recipient_id})"
                    )

            if not is_owner and not is_recipient:
                logger.warning(
                    f"User {user_id} attempted to access echo {echo_id} owned by {echo.user_id} - not owner or recipient"
                )
                return None

            # Sign media URL for access
            echo = await self._sign_media_url(echo)

            # Enrich with recipient details if any
            if echo.recipient_id:
                # Reuse recipient from access check if available, otherwise fetch
                recipient = recipient_from_access_check or await self.get_recipient(
                    echo.recipient_id, echo.user_id
                )
                if recipient:
                    echo.recipient = {
                        "recipient_id": recipient.recipient_id,
                        "name": recipient.name,
                        "email": recipient.email,
                        "motif": recipient.motif,
                        "profile_image_url": await self._sign_profile_url(
                            recipient.profile_image_url
                        ),
                    }

            return echo

        except ClientError as e:
            logger.error(f"DynamoDB error getting echo: {e}")
            raise InternalServerError(f"Failed to get echo: {str(e)}")

    async def get_user_echoes(
        self,
        user_id: str,
        category: Optional[str] = None,
        recipient_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Echo], Optional[str]]:
        """
        Get echoes for a user (vault view), one page at a time.

        Pagination contract:
            - ``limit`` clamped to ``[1, MAX_PAGE_LIMIT]``; defaults to
              ``DEFAULT_PAGE_LIMIT`` when not provided.
            - ``cursor`` is the ``next_cursor`` returned by a previous call.
              Pass ``None`` for the first page.
            - The returned ``next_cursor`` is ``None`` when there are no
              more rows on the underlying GSI.

        Filtering caveat: status/category/recipient/deleted filters are
        applied AFTER DynamoDB returns the page. Heavily-filtered queries
        therefore return short pages even when more rows exist on the
        index. Clients should keep paging while ``next_cursor`` is not
        ``None``, not while ``len(data) == limit``.

        Args:
            user_id: User ID
            category: Filter by category (post-Query)
            recipient_id: Filter by recipient (post-Query)
            status: Filter by status (post-Query)
            limit: Page size (1..MAX_PAGE_LIMIT, default DEFAULT_PAGE_LIMIT)
            cursor: Opaque cursor from a previous call

        Returns:
            Tuple of (echoes for this page, next_cursor or None)
        """
        try:
            page_limit = _clamp_limit(limit)
            exclusive_start_key = decode_cursor(cursor)

            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)

            query_kwargs: Dict[str, Any] = {
                "IndexName": "user-echoes-index",
                "KeyConditionExpression": "user_id = :user_id",
                "ExpressionAttributeValues": {":user_id": user_id},
                "Limit": page_limit,
            }
            if exclusive_start_key:
                query_kwargs["ExclusiveStartKey"] = exclusive_start_key

            response = await table.query(**query_kwargs)

            echoes: List[Echo] = []
            for item in response.get("Items", []):
                echo = Echo.from_dynamodb_item(item)

                # Apply filters
                if echo.deleted_at is not None:
                    continue
                if category and echo.category != category:
                    continue
                if recipient_id and echo.recipient_id != recipient_id:
                    continue
                if status and echo.status.value != status:
                    continue

                # NOTE: media_url is deliberately NOT signed in the vault
                # list path — the route response omits it. The detail
                # endpoint (GET /echoes/{id}) signs media_url on demand
                # when the player actually needs the URL. Eliminates N
                # wasted presigns per page.
                echoes.append(echo)

            next_cursor = encode_cursor(response.get("LastEvaluatedKey"))

            # Batch-fetch recipient details. Old code did one GetItem per
            # echo (N+1) — for a 50-echo page with 30 unique recipients
            # this collapses 30 round-trips into one BatchGetItem call.
            await self._enrich_echoes_with_recipients(echoes, user_id)

            # Sign poster URLs in parallel so the vault list can render
            # video thumbnails directly from the response (without a
            # follow-up detail-fetch).
            await self._sign_poster_urls_for_echoes(echoes)

            return echoes, next_cursor

        except ClientError as e:
            logger.error(f"DynamoDB error getting user echoes: {e}")
            raise InternalServerError(f"Failed to get echoes: {str(e)}")

    async def _enrich_echoes_with_recipients(
        self, echoes: List[Echo], owner_user_id: str
    ) -> None:
        """Populate ``echo.recipient`` for every echo with a ``recipient_id``.

        Old code: one ``GetItem`` per echo inside the iteration loop
        (classic N+1). For a 50-echo page with 30 unique recipients that
        was 30 sequential round-trips on a cold cache.

        Current code:
        1. Distinct recipient_ids fanned out via BatchGetItem (chunks of 100).
        2. Distinct profile_image_url presigns done once each via
           ``_sign_profile_urls`` (dedupe + parallel). The old serial
           ``await self._sign_profile_url`` inside the per-echo loop was
           the dominant tail-latency contributor on a warm cache: each
           ``generate_presigned_url`` call adds ~10-30 ms of awaiting on
           the aioboto3 client. 30 unique recipients = ~600 ms serial vs.
           ~30 ms parallel.
        """
        recipient_ids = [
            echo.recipient_id for echo in echoes if echo.recipient_id is not None
        ]
        if not recipient_ids:
            return

        distinct_ids = list({rid for rid in recipient_ids if rid})
        if not distinct_ids:
            return

        recipients_by_id = await self._batch_get_recipients(distinct_ids, owner_user_id)

        # Defense-in-depth ownership filter — same as before.
        recipients_by_id = {
            rid: r for rid, r in recipients_by_id.items() if r.user_id == owner_user_id
        }

        # Dedupe + parallel-sign distinct profile URLs.
        distinct_profile_urls = {
            r.profile_image_url
            for r in recipients_by_id.values()
            if r.profile_image_url
        }
        signed_by_url = await self._sign_profile_urls(distinct_profile_urls)

        for echo in echoes:
            if not echo.recipient_id:
                continue
            recipient = recipients_by_id.get(echo.recipient_id)
            if recipient is None:
                continue
            canonical_profile = recipient.profile_image_url
            signed_profile = (
                signed_by_url.get(canonical_profile, canonical_profile)
                if canonical_profile
                else None
            )
            echo.recipient = {
                "recipient_id": recipient.recipient_id,
                "name": recipient.name,
                "email": recipient.email,
                "motif": recipient.motif,
                "profile_image_url": signed_profile,
            }

    async def _batch_get_recipients(
        self, recipient_ids: List[str], owner_user_id: str
    ) -> Dict[str, Recipient]:
        """BatchGetItem for recipients, chunked at the DDB 100-key cap and
        fanned out in parallel across chunks.

        Returns a dict keyed by recipient_id. Recipients owned by a
        different user_id are dropped (defense in depth against a
        misconfigured GSI / direct-id lookup).
        """
        if not recipient_ids:
            return {}

        # Chunk into batches of 100 (DDB BatchGetItem hard cap).
        chunks = [
            recipient_ids[i : i + BATCH_GET_ITEM_MAX]
            for i in range(0, len(recipient_ids), BATCH_GET_ITEM_MAX)
        ]

        dynamodb = await self._get_dynamodb_resource()

        async def fetch_chunk(chunk: List[str]) -> List[Dict[str, Any]]:
            request_keys = [{"recipient_id": rid} for rid in chunk]
            request_items: Dict[str, Any] = {
                self.recipients_table: {"Keys": request_keys}
            }
            collected: List[Dict[str, Any]] = []
            # Loop on UnprocessedKeys — DDB returns unprocessed items when
            # throttled. Adaptive retries in Config handle the backoff;
            # we just need to drain anything remaining. Cap the outer
            # loop at 5 rounds: a persistently-throttled DDB shouldn't
            # spin a Lambda all the way to its 30s timeout.
            for _attempt in range(5):
                if not request_items:
                    break
                resp = await dynamodb.batch_get_item(RequestItems=request_items)
                collected.extend(
                    resp.get("Responses", {}).get(self.recipients_table, [])
                )
                unprocessed = resp.get("UnprocessedKeys") or {}
                if unprocessed:
                    request_items = unprocessed
                else:
                    break
            return collected

        chunk_results = await asyncio.gather(*[fetch_chunk(chunk) for chunk in chunks])

        result: Dict[str, Recipient] = {}
        for items in chunk_results:
            for item in items:
                try:
                    recipient = Recipient.from_dynamodb_item(item)
                except Exception as e:
                    logger.warning(
                        f"Skipping malformed recipient row in batch result: {e}"
                    )
                    continue
                if recipient.user_id != owner_user_id:
                    continue
                if recipient.deleted_at is not None:
                    continue
                result[recipient.recipient_id] = recipient
        return result

    async def get_received_echoes(
        self,
        user_id: str,
        category: Optional[str] = None,
        sender_id: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Echo], Optional[str]]:
        """
        Get echoes received by a user (inbox view).
        Only returns RELEASED echoes.

        Matches recipient rows by recipient_user_id (Cognito sub) via the
        recipient-user-id-index GSI. This is authoritative and works regardless
        of whether the JWT carries an email claim. Recipient rows added before
        the recipient signed up are back-linked at confirmation time (see
        echo_service.link_user_recipient_records) and via the offline backfill
        script in scripts/backfill_recipient_user_id.py.

        Performance: the per-recipient Queries that previously ran
        sequentially (Wave 1B-E4 / 50-recipient inbox = 51 sequential
        round-trips → ~10 s) now fan out via ``asyncio.gather``. End-to-end
        latency falls to roughly one round-trip.

        Pagination: the cursor pages the *recipient-rows* query, NOT the
        per-recipient echoes query. In practice the page boundary is
        almost always the natural end of the recipients list, since a
        single user rarely has more than a few hundred recipient links.
        The post-gather list is sorted by created_at descending and
        truncated to ``limit`` before being returned.

        Args:
            user_id: Logged-in user's Cognito sub
            category: Filter by category (post-fetch)
            sender_id: Filter by sender (post-fetch)
            limit: Page size (1..MAX_PAGE_LIMIT, default DEFAULT_PAGE_LIMIT)
            cursor: Opaque cursor from a previous call

        Returns:
            Tuple of (echoes for this page, next_cursor or None)
        """
        if not user_id:
            raise ValidationError("user_id is required for inbox lookup")

        try:
            page_limit = _clamp_limit(limit)
            exclusive_start_key = decode_cursor(cursor)

            dynamodb = await self._get_dynamodb_resource()
            recipients_table = await dynamodb.Table(self.recipients_table)

            recipients_query_kwargs: Dict[str, Any] = {
                "IndexName": "recipient-user-id-index",
                "KeyConditionExpression": "recipient_user_id = :uid",
                "ExpressionAttributeValues": {":uid": user_id},
                "Limit": page_limit,
            }
            if exclusive_start_key:
                recipients_query_kwargs["ExclusiveStartKey"] = exclusive_start_key

            recipient_response = await recipients_table.query(**recipients_query_kwargs)
            recipient_items = [
                item
                for item in recipient_response.get("Items", [])
                if item.get("deleted_at") is None
            ]
            recipient_ids = [item["recipient_id"] for item in recipient_items]
            next_cursor = encode_cursor(recipient_response.get("LastEvaluatedKey"))

            if not recipient_ids:
                logger.info(f"Inbox: no recipient records linked to user {user_id}")
                return [], next_cursor

            logger.info(
                f"Inbox: found {len(recipient_ids)} recipient records for user {user_id}"
            )

            # Query released echoes for each matched recipient.
            # recipient-echoes-index has hash=recipient_id only (no sort key),
            # so status must be applied as a FilterExpression rather than a
            # second KeyConditionExpression — DynamoDB rejects a 2-condition
            # KCE against a 1-attribute key schema with ValidationException.
            #
            # asyncio.gather() fans these out in parallel. Old code looped
            # sequentially; 50 recipients × ~200 ms/Query = 10 s wall time.
            # Parallel: ~one Query latency total.
            echoes_table = await dynamodb.Table(self.echoes_table)

            async def query_recipient(rid: str) -> List[Echo]:
                response = await echoes_table.query(
                    IndexName="recipient-echoes-index",
                    KeyConditionExpression="recipient_id = :rid",
                    FilterExpression="#status = :released",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":rid": rid,
                        ":released": EchoStatus.RELEASED.value,
                    },
                )

                results: List[Echo] = []
                for item in response.get("Items", []):
                    echo = Echo.from_dynamodb_item(item)
                    if category and echo.category != category:
                        continue
                    if sender_id and echo.user_id != sender_id:
                        continue
                    results.append(echo)
                return results

            per_recipient_results = await asyncio.gather(
                *[query_recipient(rid) for rid in recipient_ids]
            )

            echoes: List[Echo] = []
            for sublist in per_recipient_results:
                echoes.extend(sublist)

            # Restore deterministic ordering — gather() doesn't guarantee
            # cross-recipient time order. Newest first matches old behavior
            # (the loop preserved per-recipient order, but with one
            # recipient per page that came out approximately newest-first).
            echoes.sort(key=lambda e: e.created_at or "", reverse=True)

            # Truncate to the requested page size. The cursor pages the
            # *recipient-rows* query, but a single page of recipients can
            # fan out to many echoes (e.g. 5 recipients × 10 echoes each =
            # 50 echoes). Without this clamp the response can exceed
            # ``limit`` by an order of magnitude, defeating the pagination
            # contract clients depend on.
            echoes = echoes[:page_limit]

            # Sign poster URLs for the page so inbox cards render video
            # thumbnails directly. Same pattern as get_user_echoes.
            await self._sign_poster_urls_for_echoes(echoes)

            logger.info(f"Inbox: returning {len(echoes)} released echoes")
            return echoes, next_cursor

        except ClientError as e:
            logger.error(f"DynamoDB error getting received echoes: {e}")
            raise InternalServerError(f"Failed to get inbox: {str(e)}")

    async def update_echo(
        self, echo_id: str, user_id: str, data: Dict[str, Any]
    ) -> Echo:
        """
        Update an echo.

        Args:
            echo_id: Echo ID
            user_id: User ID (for authorization)
            data: Fields to update

        Returns:
            Updated Echo
        """
        try:
            echo = await self.get_echo(echo_id, user_id)
            if not echo:
                raise NotFoundError(f"Echo {echo_id} not found")

            # Special case: Allow media attachment on RELEASED echoes (first-time only)
            is_media_only_update = (
                "media_url" in data
                and set(data.keys()) <= {"media_url", "echo_type"}
                and not echo.media_url  # Only if media_url is currently empty
            )

            # Prevent updates to locked/released echoes (except first-time media attachment)
            if echo.status != EchoStatus.DRAFT and not is_media_only_update:
                logger.warning(
                    f"Attempted to update non-draft echo {echo_id} (status={echo.status.value})"
                )
                raise InternalServerError("Cannot update locked or released echo")

            # Apply updates
            if "title" in data:
                echo.title = data["title"]
            if "category" in data:
                echo.category = data["category"]
            if "content" in data:
                echo.content = data["content"]
            if "media_url" in data:
                # Reject presigned-URL writebacks. `_sign_media_url` mutates
                # echo.media_url in place when returning data to clients, so
                # naive client code can round-trip the presigned URL right
                # back into update_echo. Persisting that would put a
                # short-lived URL into the canonical row and break future
                # read paths once it expires (and corrupt the presign loop
                # which extracts the key by splitting on 'amazonaws.com/').
                candidate = data["media_url"]
                if _looks_like_presigned_url(candidate):
                    logger.warning(
                        f"update_echo refused presigned media_url for {echo_id}"
                    )
                    raise ValidationError(
                        "media_url must be the canonical S3 URL, not a "
                        "presigned URL. Use POST /echoes/{id}/finalize-media."
                    )
                echo.media_url = candidate
                logger.info(
                    f"Attached media to echo {echo_id} (status={echo.status.value})"
                )
            if "echo_type" in data:
                try:
                    echo.echo_type = EchoType(data["echo_type"])
                except (ValueError, KeyError):
                    pass  # Keep existing type if invalid
            if "recipient_id" in data:
                echo.recipient_id = data["recipient_id"]
            if "release_date" in data:
                # Explicit None clears the schedule (used by "Cancel
                # scheduled send" in the app); a string sets/replaces it.
                echo.release_date = data["release_date"]
            if "letter_to_recipient" in data:
                # Same set-or-clear semantics as release_date.
                echo.letter_to_recipient = data["letter_to_recipient"]

            echo.updated_at = _current_timestamp()

            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)
            await table.put_item(Item=echo.to_dynamodb_item())

            logger.info(f"Updated echo {echo_id}")
            return echo

        except (NotFoundError, InternalServerError, ValidationError):
            raise
        except Exception as e:
            logger.error(f"Error updating echo: {e}")
            raise InternalServerError(f"Failed to update echo: {str(e)}")

    async def delete_echo(self, echo_id: str, user_id: str) -> bool:
        """
        Soft delete an echo.

        Args:
            echo_id: Echo ID
            user_id: User ID (for authorization)

        Returns:
            True if deleted
        """
        try:
            echo = await self.get_echo(echo_id, user_id)
            if not echo:
                return False

            echo.deleted_at = _current_timestamp()
            echo.updated_at = _current_timestamp()

            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)
            await table.put_item(Item=echo.to_dynamodb_item())

            logger.info(f"Soft deleted echo {echo_id}")
            return True

        except Exception as e:
            logger.error(f"Error deleting echo: {e}")
            return False

    async def release_echo(self, echo_id: str, user_id: str) -> Echo:
        """
        Directly release an echo to its recipient (no-guardian path).

        Rules
        -----
        - Echo must exist and be owned by user_id.
        - Echo must have a recipient_id.
        - Echo must NOT have a guardian_id (those go through the guardian flow).
        - Echo must be in DRAFT status (LOCKED / RELEASED are rejected).

        After validation:
        1. Call echo.release() to set status = RELEASED.
        2. Persist updated echo to DynamoDB.
        3. Fire send_echo_notification to the recipient (fire-and-forget).

        Args:
            echo_id: ID of the echo to release.
            user_id: Authenticated caller's user ID (ownership check).

        Returns:
            The updated Echo with status RELEASED.

        Raises:
            NotFoundError: Echo does not exist or is not owned by user_id.
            ValidationError: Echo fails one of the pre-release checks.
        """
        echo = await self.get_echo(echo_id, user_id)
        if not echo:
            raise NotFoundError(f"Echo {echo_id} not found")

        if not echo.recipient_id:
            raise ValidationError(
                "Echo has no recipient — cannot release without a recipient"
            )

        if echo.guardian_id:
            raise ValidationError(
                "Echo has a guardian assigned — use the guardian release flow"
            )

        if echo.status == EchoStatus.RELEASED:
            raise ValidationError("Already released — echo has already been released")

        if echo.status == EchoStatus.LOCKED:
            raise ValidationError("Locked echo must be released via guardian flow")

        # Transition to RELEASED
        echo.release()

        # Persist to DynamoDB
        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)
            await table.put_item(Item=echo.to_dynamodb_item())
        except Exception as e:
            logger.error(f"DynamoDB error persisting released echo {echo_id}: {e}")
            raise InternalServerError(f"Failed to persist released echo: {str(e)}")

        logger.info(f"Echo {echo_id} released to recipient {echo.recipient_id}")

        # Fire-and-forget notification email
        try:
            recipient = await self.get_recipient(echo.recipient_id, user_id)
            if recipient:
                # Check if recipient is registered (has recipient_user_id)
                is_registered = recipient.recipient_user_id is not None

                media_fields = await self.build_email_media_fields(
                    echo, recipient.recipient_id
                )
                await email_service.send_echo_notification(
                    recipient_email=recipient.email,
                    recipient_name=recipient.name,
                    sender_name=user_id,  # Caller's display name not available here;
                    # use user_id as fallback — routes layer can enrich if desired
                    echo_title=echo.title,
                    echo_category=echo.category,
                    echo_type=echo.echo_type.value,
                    is_registered=is_registered,
                    quote=echo.letter_to_recipient or echo.content,
                    echo_date=echo.release_date or echo.updated_at,
                    **media_fields,
                )
                logger.info(
                    f"Sent echo notification for {echo_id} (registered={is_registered})"
                )
        except Exception as e:
            logger.warning(f"Failed to send echo notification for echo {echo_id}: {e}")

        return echo

    async def release_due_echoes(self) -> Dict[str, Any]:
        """
        Find every DRAFT echo whose `release_date` has passed and release it.

        Used by the hourly scheduler Lambda (`echo_release_job.lambda_handler`)
        to close the gap left by `create_echo` — that path only auto-releases
        when the echo is created with a past/now release_date; future dates
        are left in DRAFT until something else picks them up. That "something
        else" is this method.

        Implementation notes:

        - Uses a DynamoDB Scan with a FilterExpression on status + release_date.
          Acceptable at current data volumes; if the table grows large, add a
          GSI on `status` and switch this to a query.
        - Per-echo release calls are guarded so one failure does not abort
          the whole batch. Failures are counted and logged but not raised.
        - Skips items where `release_date` is missing (no schedule = nothing
          for the scheduler to act on; manual release path is unaffected).
        - The release_echo call enforces the standard preconditions
          (recipient required, no guardian, still DRAFT) — anything not
          eligible falls into the failed count and is logged.

        Returns:
            Dict with keys: `scanned` (raw item count), `released`,
            `skipped` (no recipient / no schedule / guardian-locked),
            `failed`, `errors` (truncated list of error strings).
        """
        from boto3.dynamodb.conditions import Attr

        now_iso = _current_timestamp()
        scanned = released = skipped = failed = 0
        errors: List[str] = []

        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)

            # Page through scan results — DynamoDB caps single-page size
            # to 1MB, so we loop on ExclusiveStartKey for large tables.
            scan_kwargs: Dict[str, Any] = {
                "FilterExpression": (
                    Attr("status").eq(EchoStatus.DRAFT.value)
                    & Attr("release_date").lte(now_iso)
                    & Attr("deleted_at").not_exists()
                ),
            }

            while True:
                response = await table.scan(**scan_kwargs)
                for item in response.get("Items", []):
                    scanned += 1
                    echo_id = item.get("echo_id")
                    owner_user_id = item.get("user_id")

                    if not echo_id or not owner_user_id:
                        failed += 1
                        errors.append(
                            f"Malformed echo row missing echo_id/user_id: "
                            f"{item.get('echo_id', '<no id>')}"
                        )
                        continue

                    # Items with a guardian go through the guardian flow;
                    # release_echo rejects them with ValidationError, but
                    # filtering early keeps the log clean.
                    if item.get("guardian_id"):
                        skipped += 1
                        continue
                    if not item.get("recipient_id"):
                        skipped += 1
                        continue

                    try:
                        await self.release_echo(echo_id, owner_user_id)
                        released += 1
                        logger.info(
                            f"Auto-released echo {echo_id} (owner={owner_user_id}, "
                            f"release_date={item.get('release_date')})"
                        )
                    except Exception as e:
                        failed += 1
                        msg = f"Failed to release echo {echo_id}: {e}"
                        logger.warning(msg)
                        # Bound the errors list so a bad batch doesn't
                        # produce an unreadable response payload.
                        if len(errors) < 20:
                            errors.append(msg)

                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key

        except ClientError as e:
            logger.error(f"DynamoDB error scanning for due echoes: {e}", exc_info=True)
            raise InternalServerError(f"Scheduler scan failed: {str(e)}")

        return {
            "scanned": scanned,
            "released": released,
            "skipped": skipped,
            "failed": failed,
            "errors": errors,
        }

    async def lock_echo(self, echo_id: str, user_id: str) -> Echo:
        """
        Lock an echo with a guardian, preventing further edits and notifying the guardian.

        Rules
        -----
        - Echo must exist and be owned by user_id.
        - Echo must have a guardian_id assigned.
        - Echo must be in DRAFT status (LOCKED / RELEASED are rejected).

        After validation:
        1. Call echo.lock() to set status = LOCKED and lock_date.
        2. Persist updated echo to DynamoDB.
        3. Send guardian notification email (fire-and-forget).

        Args:
            echo_id: ID of the echo to lock.
            user_id: Authenticated caller's user ID (ownership check).

        Returns:
            The updated Echo with status LOCKED.

        Raises:
            NotFoundError: Echo does not exist or is not owned by user_id.
            ValidationError: Echo fails one of the pre-lock checks.
        """
        echo = await self.get_echo(echo_id, user_id)
        if not echo:
            raise NotFoundError(f"Echo {echo_id} not found")

        if not echo.guardian_id:
            raise ValidationError(
                "Echo has no guardian — cannot lock without a guardian"
            )

        if echo.status == EchoStatus.LOCKED:
            raise ValidationError("Echo is already locked")

        if echo.status == EchoStatus.RELEASED:
            raise ValidationError("Echo is already released — cannot lock")

        # Transition to LOCKED
        echo.lock()

        # Persist to DynamoDB
        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)
            await table.put_item(Item=echo.to_dynamodb_item())
        except Exception as e:
            logger.error(f"DynamoDB error persisting locked echo {echo_id}: {e}")
            raise InternalServerError(f"Failed to persist locked echo: {str(e)}")

        logger.info(f"Echo {echo_id} locked with guardian {echo.guardian_id}")

        # Fire-and-forget guardian notification email
        try:
            guardian = await self.get_guardian(echo.guardian_id, user_id)
            if guardian:
                await email_service.send_echo_pending_notification(
                    guardian_email=guardian.email,
                    guardian_name=guardian.name,
                    owner_name=user_id,  # Fallback to user_id; routes layer can enrich
                    echo_title=echo.title,
                    echo_category=echo.category,
                )
        except Exception as e:
            logger.warning(
                f"Failed to send guardian notification for echo {echo_id}: {e}"
            )

        return echo

    # ========================================
    # S3 PRESIGNED URL GENERATION
    # ========================================

    async def generate_upload_url(
        self,
        user_id: str,
        file_type: str,
        echo_id: Optional[str] = None,
        upload_type: str = "echo",
    ) -> Dict[str, Any]:
        """
        Generate S3 presigned URL for direct upload.

        The signed PUT pre-commits the following object headers/metadata, so
        the upload itself stamps them atomically (no second roundtrip needed):

        - ``Content-Type``                — passed through from ``file_type``.
        - ``Cache-Control``                — long-lived immutable; safe because
          our keys embed a timestamp so the bytes never change in place.
        - ``Tagging`` — ``user_id``, ``echo_id`` (when applicable), and
          ``upload_type`` for cost-allocation + lifecycle policies.
        - ``Metadata`` — same three values, plus a ``signed_at`` ISO
          timestamp so finalize-time auditing can spot stale presigns.

        Args:
            user_id: User ID
            file_type: MIME type. Must be in ``ALLOWED_UPLOAD_MIME_TYPES``.
            echo_id: Optional echo ID (required for upload_type='echo')
            upload_type: 'echo' | 'profile' | 'user_profile'
                - 'echo': echoes/{user_id}/{echo_id}_{ts}.ext
                - 'profile': profiles/{user_id}/{ts}.ext  (recipient / guardian photo)
                - 'user_profile': user_profiles/{user_id}/{ts}.ext  (own avatar)

        Returns:
            Dict with 'upload_url', 'media_url', 'key', 'bucket', 'expires_in'

        Raises:
            ValidationError: ``file_type`` is not on the upload allowlist.
        """
        # Normalize client-reported aliases (image/jpg -> image/jpeg, etc.).
        file_type = _normalize_mime(file_type)
        if file_type not in ALLOWED_UPLOAD_MIME_TYPES:
            raise ValidationError(
                f"Unsupported media type '{file_type}'. "
                f"Allowed: {sorted(ALLOWED_UPLOAD_MIME_TYPES)}"
            )

        # Defensive: upload_type controls the key prefix AND is interpolated
        # into the S3 Tagging string. An attacker-supplied value containing
        # '&' or '=' would produce a malformed tag string, and an unknown
        # value could land objects in unexpected prefixes. The route layer
        # uses pydantic Literal too, but service-level enforcement keeps
        # other callers honest.
        if upload_type not in {"echo", "profile", "user_profile"}:
            raise ValidationError(
                f"Unsupported upload_type '{upload_type}'. "
                "Allowed: ['echo', 'profile', 'user_profile']"
            )

        try:
            # Single source of truth for MIME → extension (audio/image/pdf
            # explicit, video → mp4). See _upload_extension_for.
            extension = _upload_extension_for(file_type)

            timestamp_iso = _current_timestamp()
            timestamp_compact = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

            if upload_type == "profile":
                key = f"profiles/{user_id}/{timestamp_compact}.{extension}"
            elif upload_type == "user_profile":
                key = f"user_profiles/{user_id}/{timestamp_compact}.{extension}"
            else:
                # echo — echo_id may be None for new echoes
                key = (
                    f"echoes/{user_id}/{echo_id or 'new'}_"
                    f"{timestamp_compact}.{extension}"
                )

            # Build S3 PUT Tagging string: form-encoded "k1=v1&k2=v2".
            tag_parts = [
                f"user_id={user_id}",
                f"upload_type={upload_type}",
            ]
            if echo_id:
                tag_parts.append(f"echo_id={echo_id}")
            tagging = "&".join(tag_parts)

            # Metadata is custom S3 user-defined metadata (x-amz-meta-*).
            # Keep keys lowercase + ASCII; values must be ASCII too.
            metadata = {
                "user_id": user_id,
                "upload_type": upload_type,
                "signed_at": timestamp_iso,
            }
            if echo_id:
                metadata["echo_id"] = echo_id

            put_params: Dict[str, Any] = {
                "Bucket": self.s3_bucket,
                "Key": key,
                "ContentType": file_type,
                "CacheControl": _PUT_CACHE_CONTROL,
                "Tagging": tagging,
                "Metadata": metadata,
            }

            s3 = await self._get_s3_client()
            presigned_url = await s3.generate_presigned_url(
                "put_object",
                Params=put_params,
                ExpiresIn=self.presigned_url_expiry,
            )

            # Construct the permanent media URL. Note: even with Transfer
            # Acceleration enabled we hand back the *non-accelerated*
            # canonical URL because that's what we want stored in DDB.
            # The accelerated endpoint is only used for the PUT.
            media_url = f"https://{self.s3_bucket}.s3.{self.region}.amazonaws.com/{key}"

            return {
                "upload_url": presigned_url,
                "media_url": media_url,
                "key": key,
                "bucket": self.s3_bucket,
                "expires_in": self.presigned_url_expiry,
            }

        except ClientError as e:
            logger.error(f"S3 error generating presigned URL: {e}")
            raise InternalServerError(f"Failed to generate upload URL: {str(e)}")

    async def finalize_upload(
        self,
        echo_id: str,
        user_id: str,
        key: str,
        content_type: Optional[str] = None,
        *,
        skip_media_url_check: bool = False,
    ) -> Echo:
        """Confirm a client-side S3 PUT and atomically commit ``media_url``.

        Closes a race today: the client streams to S3 then calls
        ``PATCH /echoes/:id`` with ``media_url``. If the app backgrounds
        between the PUT and the PATCH the echo row is left without media.
        Worse, the PATCH trusts whatever URL the client sends — there is
        no server-side proof the object actually exists.

        This method does both checks server-side:

        1. Verify ``key`` lives under the caller's namespace
           (``echoes/{user_id}/{echo_id}_*``,
           ``profiles/{user_id}/*``, or ``user_profiles/{user_id}/*``).
           Anything else is a tenancy escape attempt — rejected.
        2. Issue ``HeadObject`` to confirm the upload landed. Captures
           ``ContentLength`` + ``ContentType`` + ``ETag`` from S3 itself,
           ignoring whatever the client claims.
        3. Atomically commit the canonical media_url + content_type to
           the echo row.

        Args:
            echo_id: Echo to attach media to.
            user_id: Caller's user_id (ownership check on the echo).
            key: S3 object key the client just PUT to.
            content_type: Optional caller hint for ``echo_type`` resolution
                when ``HeadObject`` doesn't carry one. The persisted value
                is always S3's.

        Returns:
            The updated Echo with ``media_url`` set.

        Raises:
            NotFoundError: Echo or S3 object does not exist.
            ValidationError: Key doesn't belong to ``user_id``.
        """
        echo = await self.get_echo(echo_id, user_id)
        if not echo:
            raise NotFoundError(f"Echo {echo_id} not found")

        # get_echo grants access to BOTH owner and recipient. Only the
        # owner may finalize media — otherwise a recipient could overwrite
        # the owner's attached media with anything in their own namespace.
        # Treat ownership failure as NotFound to avoid info leakage.
        if echo.user_id != user_id:
            logger.warning(
                f"finalize_upload owner reject: caller={user_id} owner={echo.user_id} "
                f"echo={echo_id}"
            )
            raise NotFoundError(f"Echo {echo_id} not found")

        # First-write semantics: an echo's media is set exactly once. Allowing
        # re-finalize would let a caller race a successful finalize with a
        # second PUT to a different key and overwrite the canonical URL.
        # If we ever support intentional re-upload, that goes through an
        # explicit `replace_media` route with separate auditing.
        #
        # The multipart-complete path passes skip_media_url_check=True
        # because S3's CompleteMultipartUpload already succeeded by the
        # time we reach here. Re-checking would let a concurrent retry
        # race (or any non-cached @idempotent re-entry) produce a 400
        # even though the user-facing outcome is success.
        if echo.media_url and not skip_media_url_check:
            raise ValidationError("Echo media has already been finalized")

        # Tenancy check — the key must live directly under this echo's
        # namespace. The 'new_' fallback that generate_upload_url emits
        # when echo_id is None is intentionally NOT allowed here, because
        # such a key carries no binding to a specific echo and could be
        # bound to any echo the caller owns.
        expected_prefix = f"echoes/{user_id}/{echo_id}_"
        if not key.startswith(expected_prefix):
            logger.warning(
                f"finalize_upload tenancy reject: user={user_id} echo={echo_id} "
                f"key={key!r}"
            )
            raise ValidationError("Object key does not belong to this echo")

        # HeadObject — proof of life + truth source for size/type.
        try:
            s3 = await self._get_s3_client()
            head = await s3.head_object(Bucket=self.s3_bucket, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise NotFoundError(
                    f"No uploaded object at s3://{self.s3_bucket}/{key}"
                )
            # 403 / AccessDenied typically means the object exists but the
            # Lambda role can't read it (KMS gap, mis-scoped bucket policy,
            # cross-region issue). Log the real error for on-call; surface
            # a generic message to the client so we don't confirm existence.
            if code in ("403", "AccessDenied", "Forbidden"):
                logger.error(
                    f"HeadObject access denied for {key} (likely IAM/KMS gap): {e}"
                )
                raise InternalServerError("Failed to verify uploaded object")
            logger.error(f"HeadObject failed for {key}: {e}")
            raise InternalServerError("Failed to verify uploaded object")

        s3_content_type = head.get("ContentType") or content_type
        s3_size = int(head.get("ContentLength") or 0)
        s3_etag = head.get("ETag", "").strip('"')

        # Canonical media_url — same shape generate_upload_url returns.
        media_url = f"https://{self.s3_bucket}.s3.{self.region}.amazonaws.com/{key}"

        echo.media_url = media_url
        if s3_content_type:
            try:
                if s3_content_type.startswith("audio/"):
                    echo.echo_type = EchoType.AUDIO
                elif s3_content_type.startswith("video/"):
                    echo.echo_type = EchoType.VIDEO
            except (ValueError, KeyError):
                pass  # Keep existing type on parse error
        echo.updated_at = _current_timestamp()

        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)
            await table.put_item(Item=echo.to_dynamodb_item())
        except ClientError as e:
            logger.error(f"DynamoDB write failed during finalize: {e}")
            raise InternalServerError(f"Failed to commit finalized echo: {str(e)}")

        logger.info(
            f"Finalized echo {echo_id}: key={key} size={s3_size} "
            f"etag={s3_etag} content_type={s3_content_type}"
        )
        return echo

    # ========================================
    # MULTIPART UPLOAD (>50 MB files)
    # ========================================
    #
    # The single-PUT presigned URL path works for everything up to ~5 GB,
    # but in practice files above ~50 MB on cellular hit two problems:
    #
    # 1. A single TCP connection failing somewhere mid-upload throws away
    #    everything uploaded so far. With multipart, only the failed
    #    chunk retries.
    # 2. We can't upload parts in parallel, so wall-clock time is
    #    bottlenecked by single-connection bandwidth even when the link
    #    can carry more.
    #
    # The four methods below wrap S3's multipart-upload API:
    #   - initiate_multipart_upload: creates the upload, returns upload_id.
    #   - generate_multipart_part_urls: presigns one URL per part.
    #   - complete_multipart_upload: assembles the parts + atomically
    #     commits media_url (reuses finalize_upload's HEAD + DDB write).
    #   - abort_multipart_upload: cleans up on client-side error.
    #
    # Abandoned uploads are also reaped by the bucket-level
    # LifecycleConfiguration in serverless.yml (7-day TTL) so a misbehaving
    # client can't run up storage charges by leaving partial uploads
    # behind.

    # 1-indexed; S3 hard cap is 10000.
    MULTIPART_MAX_PART_NUMBER = 10_000
    # Cap how many presigned URLs we hand out per call. The client's
    # default 5 MB part size with parallelism=4 only needs a handful at
    # a time; large batches just bloat the response. The client can
    # always re-request more.
    MULTIPART_PART_URL_BATCH_MAX = 1000

    async def initiate_multipart_upload(
        self,
        echo_id: str,
        user_id: str,
        file_type: str,
    ) -> Dict[str, Any]:
        """Open an S3 multipart upload for the given echo.

        Reuses the same key/metadata/tagging contract as the single-PUT
        ``generate_upload_url`` so finalize semantics are uniform: the
        bytes end up at the same canonical URL regardless of which
        upload path produced them.

        Raises:
            NotFoundError: Echo doesn't exist or isn't owned by ``user_id``.
            ValidationError: ``file_type`` isn't allowlisted, or the echo
                already has media attached.
        """
        file_type = _normalize_mime(file_type)
        if file_type not in ALLOWED_UPLOAD_MIME_TYPES:
            raise ValidationError(
                f"Unsupported media type '{file_type}'. "
                f"Allowed: {sorted(ALLOWED_UPLOAD_MIME_TYPES)}"
            )

        echo = await self.get_echo(echo_id, user_id)
        if not echo:
            raise NotFoundError(f"Echo {echo_id} not found")
        if echo.user_id != user_id:
            # NotFound, not Validation — avoid info leakage on echo
            # existence to non-owners (same posture as finalize_upload).
            logger.warning(
                f"initiate_multipart owner reject: caller={user_id} "
                f"owner={echo.user_id} echo={echo_id}"
            )
            raise NotFoundError(f"Echo {echo_id} not found")
        if echo.media_url:
            raise ValidationError("Echo media has already been finalized")

        # Same MIME → extension logic as the single-PUT path.
        extension = _upload_extension_for(file_type)

        timestamp_iso = _current_timestamp()
        timestamp_compact = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        key = f"echoes/{user_id}/{echo_id}_{timestamp_compact}.{extension}"

        tagging = "&".join(
            [
                f"user_id={user_id}",
                f"echo_id={echo_id}",
                "upload_type=echo",
            ]
        )
        metadata = {
            "user_id": user_id,
            "echo_id": echo_id,
            "upload_type": "echo",
            "signed_at": timestamp_iso,
        }

        try:
            s3 = await self._get_s3_client()
            response = await s3.create_multipart_upload(
                Bucket=self.s3_bucket,
                Key=key,
                ContentType=file_type,
                CacheControl=_PUT_CACHE_CONTROL,
                Tagging=tagging,
                Metadata=metadata,
            )
        except ClientError as e:
            logger.error(f"S3 create_multipart_upload failed for {key}: {e}")
            raise InternalServerError(f"Failed to initiate multipart upload: {str(e)}")

        upload_id = response["UploadId"]
        logger.info(
            f"Initiated multipart upload echo={echo_id} key={key!r} "
            f"upload_id={_short(upload_id)}"
        )
        return {
            "upload_id": upload_id,
            "key": key,
            "bucket": self.s3_bucket,
        }

    async def generate_multipart_part_urls(
        self,
        echo_id: str,
        user_id: str,
        upload_id: str,
        key: str,
        part_numbers: List[int],
    ) -> List[Dict[str, Any]]:
        """Issue presigned PUT URLs for the requested part numbers.

        Done in parallel via asyncio.gather — presign is local HMAC, so
        the wins are smaller than for IO-bound calls, but a 1000-part
        batch still cuts ~30 ms of awaited overhead.

        Raises:
            NotFoundError: echo doesn't exist / not owned.
            ValidationError: key doesn't belong to this echo, part_numbers
                empty, out of range, or batch larger than allowed.
        """
        if not part_numbers:
            raise ValidationError("part_numbers cannot be empty")
        if len(part_numbers) > self.MULTIPART_PART_URL_BATCH_MAX:
            raise ValidationError(
                f"part_numbers batch exceeds {self.MULTIPART_PART_URL_BATCH_MAX}"
            )
        for n in part_numbers:
            if not isinstance(n, int) or n < 1 or n > self.MULTIPART_MAX_PART_NUMBER:
                raise ValidationError(
                    f"part_number {n} out of range [1, {self.MULTIPART_MAX_PART_NUMBER}]"
                )

        # Same ownership + tenancy checks as finalize_upload. The
        # multipart-upload-id provided by S3 isn't user-scoped on its own
        # — without this guard a caller could supply someone else's
        # upload_id and start uploading parts to it.
        echo = await self.get_echo(echo_id, user_id)
        if not echo:
            raise NotFoundError(f"Echo {echo_id} not found")
        if echo.user_id != user_id:
            raise NotFoundError(f"Echo {echo_id} not found")
        expected_prefix = f"echoes/{user_id}/{echo_id}_"
        if not key.startswith(expected_prefix):
            logger.warning(
                f"multipart_part_urls tenancy reject: user={user_id} "
                f"echo={echo_id} key={key!r}"
            )
            raise ValidationError("Object key does not belong to this echo")

        try:
            s3 = await self._get_s3_client()
            urls = await asyncio.gather(
                *[
                    s3.generate_presigned_url(
                        "upload_part",
                        Params={
                            "Bucket": self.s3_bucket,
                            "Key": key,
                            "UploadId": upload_id,
                            "PartNumber": n,
                        },
                        ExpiresIn=self.presigned_url_expiry,
                    )
                    for n in part_numbers
                ]
            )
        except ClientError as e:
            logger.error(f"S3 presign upload_part failed for {key}: {e}")
            raise InternalServerError(f"Failed to generate part URLs: {str(e)}")

        return [{"part_number": n, "url": u} for n, u in zip(part_numbers, urls)]

    async def complete_multipart_upload(
        self,
        echo_id: str,
        user_id: str,
        upload_id: str,
        key: str,
        parts: List[Dict[str, Any]],
    ) -> Echo:
        """Finalize a multipart upload and atomically commit ``media_url``.

        ``parts`` is the list of ``{"part_number": int, "etag": str}``
        dicts the client collected from the per-part PUT response
        headers. S3 requires them sorted by PartNumber ascending; we
        sort defensively so a misordered client doesn't 400 on the
        CompleteMultipartUpload call.

        After S3 assembles the object we delegate to ``finalize_upload``
        which runs the same HEAD + DDB-commit path as the single-PUT
        flow. That keeps the post-upload state machine uniform.

        Raises:
            NotFoundError: echo doesn't exist / not owned.
            ValidationError: key doesn't belong, parts list empty,
                malformed, or out of part-number range.
            InternalServerError: S3 complete failed (e.g. parts size
                under 5 MB except the last — usually a client bug).
        """
        if not parts:
            raise ValidationError("parts cannot be empty")
        # Hard cap on the parts array length. Without this a malicious
        # client could POST 500,000 dicts and exhaust Lambda memory
        # before reaching any other validation.
        if len(parts) > self.MULTIPART_MAX_PART_NUMBER:
            raise ValidationError(
                f"parts list exceeds {self.MULTIPART_MAX_PART_NUMBER}"
            )
        # Defensive sort + dedup + validation in one pass. Duplicates
        # would silently overwrite each other in S3 — the second ETag
        # wins, missing parts produce a corrupt assembled object. We
        # reject loudly instead.
        normalized: List[Dict[str, Any]] = []
        seen_part_numbers: set[int] = set()
        for p in parts:
            n = p.get("part_number")
            etag = p.get("etag")
            if not isinstance(n, int) or n < 1 or n > self.MULTIPART_MAX_PART_NUMBER:
                raise ValidationError(
                    f"part_number {n} out of range [1, {self.MULTIPART_MAX_PART_NUMBER}]"
                )
            if n in seen_part_numbers:
                raise ValidationError(f"duplicate part_number {n}")
            seen_part_numbers.add(n)
            if not isinstance(etag, str) or not etag:
                raise ValidationError(f"part {n} missing etag")
            # S3 wants etags quoted; the client typically reports them
            # already-quoted from the response header. Be tolerant.
            quoted = etag if etag.startswith('"') else f'"{etag}"'
            normalized.append({"PartNumber": n, "ETag": quoted})
        normalized.sort(key=lambda p: p["PartNumber"])

        # Tenancy + ownership.
        echo = await self.get_echo(echo_id, user_id)
        if not echo:
            raise NotFoundError(f"Echo {echo_id} not found")
        if echo.user_id != user_id:
            raise NotFoundError(f"Echo {echo_id} not found")
        expected_prefix = f"echoes/{user_id}/{echo_id}_"
        if not key.startswith(expected_prefix):
            logger.warning(
                f"multipart_complete tenancy reject: user={user_id} "
                f"echo={echo_id} key={key!r}"
            )
            raise ValidationError("Object key does not belong to this echo")

        # Assemble the object on S3.
        try:
            s3 = await self._get_s3_client()
            await s3.complete_multipart_upload(
                Bucket=self.s3_bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": normalized},
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            logger.error(
                f"S3 complete_multipart_upload failed for {key} " f"(code={code}): {e}"
            )
            # NoSuchUpload usually means the abort lifecycle rule already
            # reaped the upload (client took longer than 7 days), or the
            # client passed a stale upload_id from a previous attempt.
            if code in ("NoSuchUpload",):
                raise NotFoundError("Multipart upload session expired or was aborted")
            raise InternalServerError(f"Failed to complete multipart upload: {code}")

        # Reuse the single-PUT finalize path for HEAD + atomic commit.
        # We've already done the ownership + tenancy checks above, but
        # finalize_upload re-runs them as defense-in-depth (cheap; one
        # DDB GetItem). The crucial difference: pass
        # skip_media_url_check=True because S3's CompleteMultipartUpload
        # has already succeeded — a strict first-write check here would
        # 400 on any concurrent retry that races past @idempotent's
        # cache, even though the user-facing outcome is success.
        return await self.finalize_upload(
            echo_id=echo_id,
            user_id=user_id,
            key=key,
            skip_media_url_check=True,
        )

    async def abort_multipart_upload(
        self,
        echo_id: str,
        user_id: str,
        upload_id: str,
        key: str,
    ) -> None:
        """Best-effort abort of an in-progress multipart upload.

        Used when the client gives up (user cancels, network is dead).
        ``NoSuchUpload`` is treated as success — the upload may have been
        reaped by the bucket lifecycle rule, or this is a duplicate abort
        from a retry. Either way the desired end state (no upload session
        consuming bytes) is met.
        """
        # Same tenancy/ownership posture as complete.
        echo = await self.get_echo(echo_id, user_id)
        if not echo:
            raise NotFoundError(f"Echo {echo_id} not found")
        if echo.user_id != user_id:
            raise NotFoundError(f"Echo {echo_id} not found")
        expected_prefix = f"echoes/{user_id}/{echo_id}_"
        if not key.startswith(expected_prefix):
            raise ValidationError("Object key does not belong to this echo")

        try:
            s3 = await self._get_s3_client()
            await s3.abort_multipart_upload(
                Bucket=self.s3_bucket,
                Key=key,
                UploadId=upload_id,
            )
            logger.info(
                f"Aborted multipart upload echo={echo_id} key={key!r} "
                f"upload_id={_short(upload_id)}"
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "NoSuchUpload":
                logger.info(f"abort_multipart_upload: upload already gone for {key}")
                return
            logger.error(f"S3 abort_multipart_upload failed for {key}: {e}")
            raise InternalServerError(f"Failed to abort upload: {code}")

    async def _sign_profile_url(self, url: Optional[str]) -> Optional[str]:
        """Generate presigned GET URL for a profile/avatar image stored in S3.

        Profile images live in the same private bucket as echo media.
        TTL is 12 hours — the maximum safe value for Lambda IAM role sessions.
        Called on every list/get request so the URL is always fresh.
        """
        if not url or "amazonaws.com" not in url:
            return url
        try:
            key = url.split("amazonaws.com/")[-1]
            s3 = await self._get_s3_client()
            presigned = await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.s3_bucket, "Key": key},
                ExpiresIn=43200,  # 12 h — max for temporary Lambda credentials
            )
            return presigned
        except Exception as e:
            logger.error(f"Failed to sign profile URL: {e}")
            return url  # Return original on error; image will 403 but app won't crash

    async def _sign_profile_urls(
        self, urls: "set[str] | frozenset[str]"
    ) -> Dict[str, str]:
        """Sign a batch of distinct profile URLs in parallel.

        Returns a dict mapping each input URL to its signed counterpart.
        Use over per-row ``await self._sign_profile_url(...)`` whenever
        the caller iterates over a list — collapses N serial round-trips
        into one ``asyncio.gather`` window. Failed signs fall through to
        the original URL (same contract as the singular helper).

        Args:
            urls: Distinct canonical URLs to sign. Pass a set so the
                caller can't accidentally pay for duplicates.

        Returns:
            ``{canonical_url: signed_url}``. Inputs already-signed or
            non-S3 are returned unchanged (matches singular helper).
        """
        url_list = [u for u in urls if u]
        if not url_list:
            return {}
        signed = await asyncio.gather(*[self._sign_profile_url(u) for u in url_list])
        return {orig: new for orig, new in zip(url_list, signed) if new is not None}

    async def _sign_poster_urls_for_echoes(self, echoes: List[Echo]) -> None:
        """Sign every echo's poster_url in parallel (in-place mutation).

        Unlike ``_sign_media_url`` which signs BOTH media_url and
        poster_url for an echo, this helper signs ONLY poster_url. It's
        the right tool for list endpoints — the vault list intentionally
        omits media_url to save presign cost (clients fetch detail-by-id
        when they need the playable URL), but does want to render the
        poster thumbnail directly from the list response.

        Uses the same 6h presign TTL as ``_sign_media_url``.
        """
        targets = [
            e for e in echoes if e.poster_url and "amazonaws.com" in e.poster_url
        ]
        if not targets:
            return

        async def sign_one(echo: Echo) -> None:
            if not echo.poster_url:
                return
            try:
                key = echo.poster_url.split("amazonaws.com/")[-1]
                s3 = await self._get_s3_client()
                echo.poster_url = await s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.s3_bucket, "Key": key},
                    ExpiresIn=21600,
                )
            except Exception as e:
                logger.error(f"Failed to sign poster URL for echo {echo.echo_id}: {e}")

        await asyncio.gather(*[sign_one(e) for e in targets])

    async def _sign_media_url(self, echo: Echo) -> Echo:
        """Generate presigned GET URL for secure media playback.

        TTL is 6 h. The previous 1 h was too short for users who park an
        echo open on screen, lock their phone, and come back: the player
        re-requested the URL and got a 403 once the original expired.
        6 h is also well under the 12 h Lambda IAM-role session cap, so
        Cognito Identity / STS credentials never invalidate before the
        URL does.
        """
        if echo.media_url and "amazonaws.com" in echo.media_url:
            try:
                # Extract key from URL
                # Format: https://{bucket}.s3.{region}.amazonaws.com/{key}
                key = echo.media_url.split("amazonaws.com/")[-1]

                s3 = await self._get_s3_client()
                presigned_url = await s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.s3_bucket, "Key": key},
                    ExpiresIn=21600,  # 6 h
                )
                echo.media_url = presigned_url
            except Exception as e:
                logger.error(f"Failed to sign media URL for echo {echo.echo_id}: {e}")
                # Keep original URL on error

        # Poster URL gets the same treatment when present. We sign both
        # in one helper because every caller that wants one wants the
        # other — the poster is functionally part of the same media
        # asset, just at a smaller frame.
        if echo.poster_url and "amazonaws.com" in echo.poster_url:
            try:
                key = echo.poster_url.split("amazonaws.com/")[-1]
                s3 = await self._get_s3_client()
                echo.poster_url = await s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.s3_bucket, "Key": key},
                    ExpiresIn=21600,
                )
            except Exception as e:
                logger.error(f"Failed to sign poster URL for echo {echo.echo_id}: {e}")
        return echo

    async def attach_poster(
        self,
        echo_id: str,
        user_id: str,
        key: str,
    ) -> Echo:
        """Verify a poster-image S3 PUT and atomically attach it to the echo.

        Mirrors ``finalize_upload`` but for the optional video-poster
        slot. Differences:

        - The echo must already have ``media_url`` set (the poster
          attaches to an existing media; calling this before the video
          is finalized would orphan the poster).
        - Re-attaching is allowed — clients that retry after a network
          blip just overwrite. (The original media has first-write
          semantics, but the poster is a derived asset; replacing it
          is a no-op user-visibly.)
        - Server-side ``HeadObject`` still confirms the object landed
          and the key prefix still has to belong to this echo, so the
          poster can't be forged.
        """
        echo = await self.get_echo(echo_id, user_id)
        if not echo:
            raise NotFoundError(f"Echo {echo_id} not found")
        # Owner-only (same posture as finalize_upload). Recipients see
        # NotFound rather than Forbidden — keeps echo existence private.
        if echo.user_id != user_id:
            logger.warning(
                f"attach_poster owner reject: caller={user_id} "
                f"owner={echo.user_id} echo={echo_id}"
            )
            raise NotFoundError(f"Echo {echo_id} not found")
        if not echo.media_url:
            raise ValidationError(
                "Echo has no media to attach a poster to — finalize media first"
            )
        expected_prefix = f"echoes/{user_id}/{echo_id}_"
        if not key.startswith(expected_prefix):
            logger.warning(
                f"attach_poster tenancy reject: user={user_id} echo={echo_id} "
                f"key={key!r}"
            )
            raise ValidationError("Object key does not belong to this echo")

        try:
            s3 = await self._get_s3_client()
            await s3.head_object(Bucket=self.s3_bucket, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise NotFoundError(
                    f"No uploaded poster at s3://{self.s3_bucket}/{key}"
                )
            if code in ("403", "AccessDenied", "Forbidden"):
                logger.error(
                    f"attach_poster HEAD access denied for {key} "
                    f"(likely IAM/KMS gap): {e}"
                )
                raise InternalServerError("Failed to verify uploaded poster")
            logger.error(f"attach_poster HEAD failed for {key}: {e}")
            raise InternalServerError("Failed to verify uploaded poster")

        echo.poster_url = (
            f"https://{self.s3_bucket}.s3.{self.region}.amazonaws.com/{key}"
        )
        echo.updated_at = _current_timestamp()

        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)
            await table.put_item(Item=echo.to_dynamodb_item())
        except ClientError as e:
            logger.error(f"DynamoDB write failed during attach_poster: {e}")
            raise InternalServerError(f"Failed to commit poster: {str(e)}")

        logger.info(f"Attached poster to echo {echo_id}: key={key!r}")
        return echo

    # ========================================
    # ATTACHMENTS (multiple media per echo)
    # ========================================

    def _canonical_url(self, key: str) -> str:
        """Canonical (non-presigned) S3 URL for an object key."""
        return f"https://{self.s3_bucket}.s3.{self.region}.amazonaws.com/{key}"

    @staticmethod
    def _attachment_type_for(content_type: Optional[str], key: str) -> AttachmentType:
        """Classify an attachment from its content-type, falling back to ext."""
        ct = (content_type or "").lower()
        if ct.startswith("image/"):
            return AttachmentType.IMAGE
        if ct.startswith("video/"):
            return AttachmentType.VIDEO
        if ct.startswith("audio/"):
            return AttachmentType.AUDIO
        lowered = key.lower()
        if lowered.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic")):
            return AttachmentType.IMAGE
        if lowered.endswith((".mp4", ".mov", ".m4v")):
            return AttachmentType.VIDEO
        if lowered.endswith((".m4a", ".mp3", ".aac", ".wav", ".ogg")):
            return AttachmentType.AUDIO
        return AttachmentType.FILE

    async def _head_object_or_raise(
        self, key: str, content_type: Optional[str], *, what: str
    ) -> Dict[str, Any]:
        """HeadObject with the same error posture as finalize_upload."""
        try:
            s3 = await self._get_s3_client()
            return await s3.head_object(Bucket=self.s3_bucket, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                raise NotFoundError(
                    f"No uploaded {what} at s3://{self.s3_bucket}/{key}"
                )
            if code in ("403", "AccessDenied", "Forbidden"):
                logger.error(f"HeadObject access denied for {what} {key}: {e}")
                raise InternalServerError(f"Failed to verify uploaded {what}")
            logger.error(f"HeadObject failed for {what} {key}: {e}")
            raise InternalServerError(f"Failed to verify uploaded {what}")

    async def _presign_get(
        self, url: Optional[str], expires: int = 21600
    ) -> Optional[str]:
        """Presign a canonical S3 URL for GET. No-op for non-S3 / already-signed.

        Guards against double-signing: a URL that already carries query params
        (a presigned URL) is returned untouched, so callers can pass an echo
        whose media has already been signed by ``get_echo`` without corrupting
        the key.
        """
        if not url or "amazonaws.com" not in url:
            return url
        if "?" in url or "X-Amz-Signature" in url:
            return url  # already presigned
        try:
            key = url.split("amazonaws.com/")[-1]
            s3 = await self._get_s3_client()
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.s3_bucket, "Key": key},
                ExpiresIn=expires,
            )
        except Exception as e:
            logger.error(f"Failed to presign URL {url!r}: {e}")
            return url

    async def add_attachment(
        self,
        echo_id: str,
        user_id: str,
        key: str,
        *,
        content_type: Optional[str] = None,
        duration: Optional[str] = None,
        thumb_key: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Echo:
        """Verify an uploaded S3 object and APPEND it as an echo attachment.

        Mirrors ``finalize_upload``'s server-side guarantees (owner-only,
        tenancy prefix, HeadObject proof-of-life) but appends to
        ``echo.attachments`` instead of the single ``media_url`` slot — so an
        echo can carry a text message plus multiple photos/videos/voice notes.

        The echo is loaded RAW (no URL signing) so existing attachments keep
        their canonical URLs when we persist; signing happens only on read.

        Args:
            echo_id: Echo to attach to.
            user_id: Caller (must own the echo).
            key: S3 object key the client just PUT to.
            content_type: Caller hint when HeadObject has none.
            duration: Optional display duration ("2:32") for audio/video.
            thumb_key: Optional poster/preview object key (same namespace).
            filename: Optional original filename (for FILE attachments).

        Returns:
            The updated Echo (attachments carry canonical URLs).
        """
        # Raw load (no signing) + owner check.
        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)
            response = await table.get_item(Key={"echo_id": echo_id})
        except ClientError as e:
            logger.error(f"DynamoDB error loading echo for add_attachment: {e}")
            raise InternalServerError(f"Failed to load echo: {str(e)}")
        if "Item" not in response:
            raise NotFoundError(f"Echo {echo_id} not found")
        echo = Echo.from_dynamodb_item(response["Item"])
        if echo.user_id != user_id:
            logger.warning(
                f"add_attachment owner reject: caller={user_id} "
                f"owner={echo.user_id} echo={echo_id}"
            )
            raise NotFoundError(f"Echo {echo_id} not found")

        # Tenancy: object must live under this echo's namespace.
        expected_prefix = f"echoes/{user_id}/{echo_id}_"
        if not key.startswith(expected_prefix):
            logger.warning(
                f"add_attachment tenancy reject: user={user_id} echo={echo_id} "
                f"key={key!r}"
            )
            raise ValidationError("Object key does not belong to this echo")

        head = await self._head_object_or_raise(key, content_type, what="attachment")
        s3_content_type = head.get("ContentType") or content_type
        s3_size = int(head.get("ContentLength") or 0)

        # Optional thumbnail (poster for video / preview for image).
        thumb_url: Optional[str] = None
        if thumb_key:
            if not thumb_key.startswith(expected_prefix):
                raise ValidationError("Thumbnail key does not belong to this echo")
            await self._head_object_or_raise(thumb_key, None, what="thumbnail")
            thumb_url = self._canonical_url(thumb_key)

        att_type = self._attachment_type_for(s3_content_type, key)
        media_url = self._canonical_url(key)
        echo.attachments.append(
            Attachment(
                type=att_type,
                media_url=media_url,
                thumb_url=thumb_url,
                mime_type=s3_content_type,
                size_bytes=s3_size or None,
                duration=duration,
                filename=filename,
            )
        )

        # Back-compat: mirror the first audio/video attachment into the legacy
        # media_url/echo_type slot and the first poster/image into poster_url,
        # so single-media readers and the email hero keep working unchanged.
        if (
            att_type in (AttachmentType.AUDIO, AttachmentType.VIDEO)
            and not echo.media_url
        ):
            echo.media_url = media_url
            echo.echo_type = (
                EchoType.AUDIO if att_type == AttachmentType.AUDIO else EchoType.VIDEO
            )
        if not echo.poster_url:
            if thumb_url:
                echo.poster_url = thumb_url
            elif att_type == AttachmentType.IMAGE:
                echo.poster_url = media_url

        echo.updated_at = _current_timestamp()
        try:
            await table.put_item(Item=echo.to_dynamodb_item())
        except ClientError as e:
            logger.error(f"DynamoDB write failed during add_attachment: {e}")
            raise InternalServerError(f"Failed to commit attachment: {str(e)}")

        logger.info(
            f"Added {att_type.value} attachment to echo {echo_id}: key={key} "
            f"size={s3_size} content_type={s3_content_type}"
        )
        return echo

    async def remove_attachment(
        self, echo_id: str, user_id: str, attachment_id: str
    ) -> Echo:
        """Remove an attachment from a DRAFT echo (owner-only).

        Only editable while the echo is still a DRAFT (not yet sent). Hard-removes
        the attachment from the list — the S3 object is left for lifecycle cleanup
        (the echo was never delivered, so this isn't the soft-delete case). The
        legacy media_url/poster_url/echo_type mirror fields are recomputed from
        whatever attachments remain so single-media readers stay consistent.
        """
        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)
            response = await table.get_item(Key={"echo_id": echo_id})
        except ClientError as e:
            logger.error(f"DynamoDB error loading echo for remove_attachment: {e}")
            raise InternalServerError(f"Failed to load echo: {str(e)}")
        if "Item" not in response:
            raise NotFoundError(f"Echo {echo_id} not found")
        echo = Echo.from_dynamodb_item(response["Item"])
        if echo.user_id != user_id:
            logger.warning(
                f"remove_attachment owner reject: caller={user_id} "
                f"owner={echo.user_id} echo={echo_id}"
            )
            raise NotFoundError(f"Echo {echo_id} not found")
        if echo.status != EchoStatus.DRAFT:
            raise ValidationError("Only draft echoes can be edited")

        before = len(echo.attachments)
        echo.attachments = [
            a for a in echo.attachments if a.attachment_id != attachment_id
        ]
        if len(echo.attachments) == before:
            raise NotFoundError(f"Attachment {attachment_id} not found")

        # Recompute the legacy mirror fields from the remaining attachments.
        first_av = next(
            (
                a
                for a in echo.attachments
                if a.type in (AttachmentType.AUDIO, AttachmentType.VIDEO)
            ),
            None,
        )
        if first_av:
            echo.media_url = first_av.media_url
            echo.echo_type = (
                EchoType.AUDIO
                if first_av.type == AttachmentType.AUDIO
                else EchoType.VIDEO
            )
        else:
            echo.media_url = None
            echo.echo_type = EchoType.TEXT
        echo.poster_url = next(
            (a.thumb_url for a in echo.attachments if a.thumb_url),
            next(
                (
                    a.media_url
                    for a in echo.attachments
                    if a.type == AttachmentType.IMAGE
                ),
                None,
            ),
        )

        echo.updated_at = _current_timestamp()
        try:
            await table.put_item(Item=echo.to_dynamodb_item())
        except ClientError as e:
            logger.error(f"DynamoDB write failed during remove_attachment: {e}")
            raise InternalServerError(f"Failed to commit removal: {str(e)}")

        logger.info(f"Removed attachment {attachment_id} from echo {echo_id}")
        return echo

    async def sign_attachments(self, echo: Echo) -> Echo:
        """Presign every attachment's media_url + thumb_url in place (6h TTL).

        Call at the response boundary only — never before a persist, or signed
        URLs get written back to DynamoDB.
        """
        for att in echo.attachments:
            signed = await self._presign_get(att.media_url)
            if signed:
                att.media_url = signed
            if att.thumb_url:
                att.thumb_url = await self._presign_get(att.thumb_url)
        return echo

    async def build_email_media_fields(
        self, echo: Echo, recipient_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Derive the email template's media fields from an echo's attachments.

        Returns keys understood by ``email_service.send_echo_notification``:
        ``attachment_count``, ``media_duration``, ``hero_image_url``,
        ``attachment_thumb_url``, and (when ``recipient_id`` is given)
        ``open_echo_url`` / ``attachment_url`` pointing at the tokenized public
        share viewer so the recipient can play/download every attachment
        without the app and without stale links.

        Hero/thumb URLs are presigned with the 7-day SigV4 max; emails opened
        later fall back to the template's default asset.
        """
        atts = echo.attachments or []
        fields: Dict[str, Any] = {"attachment_count": len(atts)}

        if recipient_id:
            token = create_share_token(echo.echo_id, recipient_id)
            share_url = build_share_url(echo.echo_id, token)
            fields["open_echo_url"] = share_url
            fields["attachment_url"] = share_url

        primary_av = next(
            (a for a in atts if a.type in (AttachmentType.AUDIO, AttachmentType.VIDEO)),
            None,
        )
        if primary_av and primary_av.duration:
            fields["media_duration"] = primary_av.duration

        hero = next((a for a in atts if a.type == AttachmentType.IMAGE), None)
        if hero:
            thumb_source: Optional[str] = hero.thumb_url or hero.media_url
        elif primary_av and primary_av.thumb_url:
            thumb_source = primary_av.thumb_url
        else:
            # Already-signed poster from get_echo is passed through untouched.
            thumb_source = echo.poster_url

        if thumb_source:
            signed = await self._presign_get(thumb_source, expires=604800)  # 7d max
            fields["hero_image_url"] = signed
            fields["attachment_thumb_url"] = signed
        return fields

    # ========================================
    # PUBLIC SHARE (tokenized recipient viewer)
    # ========================================

    async def get_shared_echo(self, echo_id: str, recipient_id: str) -> Optional[Echo]:
        """Load an echo for the public viewer (token already verified upstream).

        No authenticated user — access is authorized by the share token, which
        binds echo_id + recipient_id. Returns the echo with CANONICAL attachment
        URLs (the viewer routes media through the redirect endpoint, not signed
        URLs). Only RELEASED, non-deleted echoes addressed to this recipient are
        shareable — never drafts.
        """
        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.echoes_table)
            response = await table.get_item(Key={"echo_id": echo_id})
        except ClientError as e:
            logger.error(f"DynamoDB error loading shared echo {echo_id}: {e}")
            return None
        if "Item" not in response:
            return None
        echo = Echo.from_dynamodb_item(response["Item"])
        if echo.recipient_id != recipient_id:
            return None
        if echo.deleted_at:
            return None
        if echo.status != EchoStatus.RELEASED:
            return None
        return echo

    async def _presign_get_with_disposition(
        self,
        url: Optional[str],
        *,
        download: bool,
        filename: Optional[str] = None,
        expires: int = 21600,
    ) -> Optional[str]:
        """Presign a canonical S3 URL; force download via Content-Disposition."""
        if not url or "amazonaws.com" not in url:
            return url
        key = url.split("amazonaws.com/")[-1]
        params: Dict[str, Any] = {"Bucket": self.s3_bucket, "Key": key}
        if download:
            name = filename or key.split("/")[-1]
            params["ResponseContentDisposition"] = f'attachment; filename="{name}"'
        try:
            s3 = await self._get_s3_client()
            return await s3.generate_presigned_url(
                "get_object", Params=params, ExpiresIn=expires
            )
        except Exception as e:
            logger.error(f"Failed to presign shared URL {url!r}: {e}")
            return None

    async def presign_shared_attachment(
        self,
        echo_id: str,
        recipient_id: str,
        attachment_id: str,
        *,
        download: bool,
    ) -> Optional[str]:
        """Resolve a shared attachment to a fresh presigned URL.

        ``attachment_id == "primary"`` resolves the legacy single ``media_url``
        (echoes created before the attachments model). Returns None if the echo
        isn't shareable or the attachment doesn't exist.
        """
        echo = await self.get_shared_echo(echo_id, recipient_id)
        if not echo:
            return None
        if attachment_id == "primary":
            url: Optional[str] = echo.media_url
            filename: Optional[str] = None
        else:
            att = next(
                (a for a in echo.attachments if a.attachment_id == attachment_id),
                None,
            )
            if not att:
                return None
            url = att.media_url
            filename = att.filename
        return await self._presign_get_with_disposition(
            url, download=download, filename=filename
        )

    # ========================================
    # RECIPIENT CRUD OPERATIONS
    # ========================================

    async def create_recipient(self, user_id: str, data: Dict[str, Any]) -> Recipient:
        """Create a new recipient."""
        try:
            email = data.get("email", "").strip().lower()

            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.recipients_table)

            # Check for existing recipient with same email for this user
            # Query by email index
            response = await table.query(
                IndexName="email-index",
                KeyConditionExpression="email = :email",
                ExpressionAttributeValues={":email": email},
            )

            for item in response.get("Items", []):
                r = Recipient.from_dynamodb_item(item)
                if r.user_id == user_id and r.deleted_at is None:
                    logger.warning(
                        f"User {user_id} attempted to add duplicate recipient email {email}"
                    )
                    raise ValidationError(
                        f"A recipient with email {email} already exists"
                    )

            # Check if recipient email matches an existing user account
            recipient_user_id = None
            logger.info(f"Checking for existing user with email: {email}")
            try:
                existing_user = await self.dynamodb_service.get_user_by_email(email)
                if existing_user:
                    recipient_user_id = existing_user.user_id
                    logger.info(
                        f"✅ Linking recipient to user account: {recipient_user_id} (email: {email})"
                    )
                else:
                    logger.info(
                        f"No existing user found for email: {email} - recipient_user_id will be None"
                    )
            except Exception as e:
                logger.error(
                    f"❌ Could not check for existing user by email ({email}): {e}",
                    exc_info=True,
                )

            recipient = Recipient(
                user_id=user_id,
                name=data.get("name", ""),
                email=email,
                recipient_user_id=recipient_user_id,
                relationship=data.get("relationship"),
                motif=data.get("motif"),
                profile_image_url=data.get("profile_image_url"),
            )

            logger.info(
                f"Creating recipient: id={recipient.recipient_id}, email={email}, "
                f"recipient_user_id={recipient_user_id or 'None (not linked)'}"
            )

            await table.put_item(Item=recipient.to_dynamodb_item())

            # Log what was actually persisted
            persisted_item = recipient.to_dynamodb_item()
            logger.info(
                f"Persisted recipient to DynamoDB: id={persisted_item.get('recipient_id')}, "
                f"has_recipient_user_id={'recipient_user_id' in persisted_item}"
            )
            if "recipient_user_id" in persisted_item:
                logger.info(
                    f"recipient_user_id value: {persisted_item['recipient_user_id']}"
                )

            logger.info(
                f"Created recipient {recipient.recipient_id} for user {user_id}"
            )
            return recipient

        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Error creating recipient: {e}")
            raise InternalServerError(f"Failed to create recipient: {str(e)}")

    async def link_user_to_recipients(self, user_id: str, email: str) -> int:
        """
        Back-link a newly-confirmed user to existing recipient rows whose
        email matches.

        Called from the signup-confirmation flow so that recipients added
        BEFORE the recipient signed up become discoverable via the
        recipient-user-id-index GSI used by the inbox query.

        Idempotent: rows already linked to this user_id are skipped; rows
        linked to a different user_id are left alone and logged.

        Args:
            user_id: New user's Cognito sub.
            email: New user's email (case-insensitive).

        Returns:
            Number of recipient rows newly linked.
        """
        if not user_id or not email:
            return 0

        normalized_email = email.strip().lower()
        if not normalized_email:
            return 0

        linked = 0
        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.recipients_table)

            response = await table.query(
                IndexName="email-index",
                KeyConditionExpression="email = :email",
                ExpressionAttributeValues={":email": normalized_email},
            )

            for item in response.get("Items", []):
                if item.get("deleted_at") is not None:
                    continue

                existing = item.get("recipient_user_id")
                if existing == user_id:
                    continue
                if existing and existing != user_id:
                    logger.warning(
                        f"Recipient {item.get('recipient_id')} already linked to "
                        f"a different user_id ({existing}); skipping back-link "
                        f"for {user_id}"
                    )
                    continue

                await table.update_item(
                    Key={"recipient_id": item["recipient_id"]},
                    UpdateExpression="SET recipient_user_id = :uid, updated_at = :ts",
                    ExpressionAttributeValues={
                        ":uid": user_id,
                        ":ts": _current_timestamp(),
                    },
                )
                linked += 1
                logger.info(
                    f"Back-linked recipient {item.get('recipient_id')} "
                    f"(email={normalized_email}) to user {user_id}"
                )

            if linked:
                logger.info(
                    f"link_user_to_recipients: linked {linked} recipient row(s) "
                    f"for user {user_id} ({normalized_email})"
                )
            else:
                logger.info(
                    f"link_user_to_recipients: no recipient rows to link for "
                    f"user {user_id} ({normalized_email})"
                )
            return linked

        except ClientError as e:
            # Don't fail the signup confirmation if back-link fails — log and
            # rely on the offline backfill script to recover.
            logger.error(f"link_user_to_recipients failed for user {user_id}: {e}")
            return linked

    async def get_user_recipients(
        self,
        user_id: str,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Recipient], Optional[str]]:
        """Get active recipients for a user, one page at a time.

        Soft-deleted rows are filtered post-Query, so a heavily-pruned
        contact list will return short pages even when more rows remain
        on the GSI. Callers should keep paging while ``next_cursor`` is
        not ``None``.
        """
        try:
            page_limit = _clamp_limit(limit)
            exclusive_start_key = decode_cursor(cursor)

            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.recipients_table)

            query_kwargs: Dict[str, Any] = {
                "IndexName": "user-recipients-index",
                "KeyConditionExpression": "user_id = :user_id",
                "ExpressionAttributeValues": {":user_id": user_id},
                "Limit": page_limit,
            }
            if exclusive_start_key:
                query_kwargs["ExclusiveStartKey"] = exclusive_start_key

            response = await table.query(**query_kwargs)

            # Filter soft-deletes, then dedupe + parallel-sign profile URLs.
            # Old code awaited self._sign_profile_url(...) inside the per-row
            # loop — serial across the page.
            recipients: List[Recipient] = [
                Recipient.from_dynamodb_item(item)
                for item in response.get("Items", [])
                if item.get("deleted_at") is None
            ]
            distinct_urls = {
                r.profile_image_url for r in recipients if r.profile_image_url
            }
            signed_by_url = await self._sign_profile_urls(distinct_urls)
            for r in recipients:
                if r.profile_image_url:
                    r.profile_image_url = signed_by_url.get(
                        r.profile_image_url, r.profile_image_url
                    )

            next_cursor = encode_cursor(response.get("LastEvaluatedKey"))
            return recipients, next_cursor

        except ClientError as e:
            logger.error(f"Error getting recipients: {e}")
            raise InternalServerError(f"Failed to get recipients: {str(e)}")

    async def get_recipient(
        self, recipient_id: str, user_id: str
    ) -> Optional[Recipient]:
        """Get a specific recipient by ID."""
        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.recipients_table)
            response = await table.get_item(Key={"recipient_id": recipient_id})

            if "Item" not in response:
                return None

            recipient = Recipient.from_dynamodb_item(response["Item"])

            # Security: Verify ownership
            if recipient.user_id != user_id:
                return None

            return recipient

        except Exception as e:
            logger.error(f"Error getting recipient {recipient_id}: {e}")
            return None

    async def delete_recipient(self, recipient_id: str, user_id: str) -> bool:
        """Soft delete a recipient."""
        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.recipients_table)

            # Get existing
            response = await table.get_item(Key={"recipient_id": recipient_id})
            if "Item" not in response:
                return False

            recipient = Recipient.from_dynamodb_item(response["Item"])
            if recipient.user_id != user_id:
                return False

            recipient.soft_delete()
            await table.put_item(Item=recipient.to_dynamodb_item())

            logger.info(f"Soft deleted recipient {recipient_id}")
            return True

        except Exception as e:
            logger.error(f"Error deleting recipient: {e}")
            return False

    # ========================================
    # GUARDIAN CRUD OPERATIONS
    # ========================================

    async def create_guardian(self, user_id: str, data: Dict[str, Any]) -> Guardian:
        """Create a new guardian."""
        try:
            email = data.get("email", "").strip().lower()

            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.guardians_table)

            # Check for existing guardian with same email for this user
            # Query by email index
            response = await table.query(
                IndexName="email-index",
                KeyConditionExpression="email = :email",
                ExpressionAttributeValues={":email": email},
            )

            for item in response.get("Items", []):
                g = Guardian.from_dynamodb_item(item)
                if g.user_id == user_id and g.deleted_at is None:
                    logger.warning(
                        f"User {user_id} attempted to add duplicate guardian email {email}"
                    )
                    raise ValidationError(
                        f"A guardian with email {email} already exists"
                    )

            guardian = Guardian(
                user_id=user_id,
                name=data.get("name", ""),
                email=email,
                scope=GuardianScope(data.get("scope", "ALL")),
                trigger=GuardianTrigger(data.get("trigger", "MANUAL")),
                profile_image_url=data.get("profile_image_url"),
            )

            await table.put_item(Item=guardian.to_dynamodb_item())

            logger.info(f"Created guardian {guardian.guardian_id} for user {user_id}")
            return guardian

        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Error creating guardian: {e}")
            raise InternalServerError(f"Failed to create guardian: {str(e)}")

    async def get_user_guardians(
        self,
        user_id: str,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Guardian], Optional[str]]:
        """Get active guardians for a user, one page at a time."""
        try:
            page_limit = _clamp_limit(limit)
            exclusive_start_key = decode_cursor(cursor)

            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.guardians_table)

            query_kwargs: Dict[str, Any] = {
                "IndexName": "user-guardians-index",
                "KeyConditionExpression": "user_id = :user_id",
                "ExpressionAttributeValues": {":user_id": user_id},
                "Limit": page_limit,
            }
            if exclusive_start_key:
                query_kwargs["ExclusiveStartKey"] = exclusive_start_key

            response = await table.query(**query_kwargs)

            # Dedupe + parallel-sign profile URLs — same shape as recipients.
            guardians: List[Guardian] = [
                Guardian.from_dynamodb_item(item)
                for item in response.get("Items", [])
                if item.get("deleted_at") is None
            ]
            distinct_urls = {
                g.profile_image_url for g in guardians if g.profile_image_url
            }
            signed_by_url = await self._sign_profile_urls(distinct_urls)
            for g in guardians:
                if g.profile_image_url:
                    g.profile_image_url = signed_by_url.get(
                        g.profile_image_url, g.profile_image_url
                    )

            next_cursor = encode_cursor(response.get("LastEvaluatedKey"))
            return guardians, next_cursor

        except ClientError as e:
            logger.error(f"Error getting guardians: {e}")
            raise InternalServerError(f"Failed to get guardians: {str(e)}")

    async def get_guardian(self, guardian_id: str, user_id: str) -> Guardian:
        """Get a specific guardian by ID."""
        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.guardians_table)

            response = await table.get_item(Key={"guardian_id": guardian_id})
            if "Item" not in response:
                raise NotFoundError(f"Guardian {guardian_id} not found")

            guardian = Guardian.from_dynamodb_item(response["Item"])
            if guardian.user_id != user_id:
                raise NotFoundError(f"Guardian {guardian_id} not found")

            if guardian.deleted_at is not None:
                raise NotFoundError(f"Guardian {guardian_id} not found")

            return guardian

        except NotFoundError:
            raise
        except ClientError as e:
            logger.error(f"Error getting guardian: {e}")
            raise InternalServerError(f"Failed to get guardian: {str(e)}")

    async def update_guardian_permissions(
        self, guardian_id: str, user_id: str, data: Dict[str, Any]
    ) -> Guardian:
        """Update guardian permissions."""
        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.guardians_table)

            response = await table.get_item(Key={"guardian_id": guardian_id})
            if "Item" not in response:
                raise NotFoundError(f"Guardian {guardian_id} not found")

            guardian = Guardian.from_dynamodb_item(response["Item"])
            if guardian.user_id != user_id:
                raise NotFoundError(f"Guardian {guardian_id} not found")

            # Apply permission updates
            scope = GuardianScope(data["scope"]) if "scope" in data else None
            trigger = GuardianTrigger(data["trigger"]) if "trigger" in data else None

            guardian.update_permissions(
                scope=scope,
                trigger=trigger,
                allowed_echo_ids=data.get("allowed_echo_ids"),
                allowed_recipient_ids=data.get("allowed_recipient_ids"),
            )

            await table.put_item(Item=guardian.to_dynamodb_item())

            logger.info(f"Updated guardian {guardian_id} permissions")
            return guardian

        except NotFoundError:
            raise
        except Exception as e:
            logger.error(f"Error updating guardian: {e}")
            raise InternalServerError(f"Failed to update guardian: {str(e)}")

    async def delete_guardian(self, guardian_id: str, user_id: str) -> bool:
        """Soft delete a guardian."""
        try:
            dynamodb = await self._get_dynamodb_resource()
            table = await dynamodb.Table(self.guardians_table)

            response = await table.get_item(Key={"guardian_id": guardian_id})
            if "Item" not in response:
                return False

            guardian = Guardian.from_dynamodb_item(response["Item"])
            if guardian.user_id != user_id:
                return False

            guardian.soft_delete()
            await table.put_item(Item=guardian.to_dynamodb_item())

            logger.info(f"Soft deleted guardian {guardian_id}")
            return True

        except Exception as e:
            logger.error(f"Error deleting guardian: {e}")
            return False


@lru_cache(maxsize=1)
def get_echo_service() -> "EchoService":
    """Process-wide EchoService singleton.

    EchoService was being instantiated 4 times (routers + controllers).
    Its __init__ wires up an aioboto3 S3 session + a DynamoDB service —
    we want exactly one of those per container.
    """
    return EchoService()
