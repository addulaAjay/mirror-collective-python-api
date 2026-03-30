"""
Echo Service for Echo Vault feature.
Handles CRUD operations for Echoes, Recipients, and Guardians.
Includes S3 presigned URL generation for media uploads.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aioboto3
from botocore.exceptions import ClientError

from ..core.exceptions import InternalServerError, NotFoundError, ValidationError
from ..models.echo import (
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


def _current_timestamp() -> str:
    """Get current UTC timestamp in ISO format"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EchoService:
    """
    Service for managing Echo Vault entities in DynamoDB.
    Also handles S3 presigned URL generation for media uploads.
    """

    def __init__(self):
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

        # Initialize DynamoDB service for user lookups
        from src.app.services.dynamodb_service import DynamoDBService

        self.dynamodb_service = DynamoDBService()

        target = "Local DynamoDB" if self.endpoint_url else "AWS DynamoDB"
        logger.info(
            f"EchoService initialized - Target: {target}, "
            f"Echoes Table: {self.echoes_table}, S3 Bucket: {self.s3_bucket}"
        )

    def _get_dynamodb_kwargs(self) -> Dict[str, Any]:
        """Get DynamoDB connection parameters (local or AWS)"""
        kwargs: Dict[str, Any] = {"region_name": self.region}

        if self.endpoint_url:
            kwargs.update(
                {
                    "endpoint_url": self.endpoint_url,
                    "aws_access_key_id": "dummy",
                    "aws_secret_access_key": "dummy",
                }
            )

        return kwargs

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
            )

            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
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
                async with self.session.resource(
                    "dynamodb", **self._get_dynamodb_kwargs()
                ) as dynamodb:
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

                            await email_service.send_echo_notification(
                                recipient_email=recipient.email,
                                recipient_name=recipient.name,
                                sender_name=user_id,  # TODO: fetch actual user name
                                echo_title=echo.title,
                                echo_category=echo.category,
                                echo_type=echo.echo_type.value,
                                is_registered=is_registered,
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
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
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
                    recipient = await self.get_recipient(
                        echo.recipient_id, echo.user_id
                    )
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
    ) -> List[Echo]:
        """
        Get all echoes for a user (vault view).

        Args:
            user_id: User ID
            category: Filter by category
            recipient_id: Filter by recipient
            status: Filter by status

        Returns:
            List of user's echoes
        """
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.echoes_table)

                # Query by user_id index
                response = await table.query(
                    IndexName="user-echoes-index",
                    KeyConditionExpression="user_id = :user_id",
                    ExpressionAttributeValues={":user_id": user_id},
                )

                echoes = []
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

                    # Sign media URL for access
                    echo = await self._sign_media_url(echo)
                    echoes.append(echo)

                # Enrich with recipient details if any
                recipient_cache = {}
                for echo in echoes:
                    if echo.recipient_id:
                        if echo.recipient_id not in recipient_cache:
                            recipient = await self.get_recipient(
                                echo.recipient_id, user_id
                            )
                            if recipient:
                                recipient_cache[echo.recipient_id] = {
                                    "recipient_id": recipient.recipient_id,
                                    "name": recipient.name,
                                    "email": recipient.email,
                                    "motif": recipient.motif,
                                }

                        echo.recipient = recipient_cache.get(echo.recipient_id)

                return echoes

        except ClientError as e:
            logger.error(f"DynamoDB error getting user echoes: {e}")
            raise InternalServerError(f"Failed to get echoes: {str(e)}")

    async def get_received_echoes(
        self,
        user_id: Optional[str] = None,
        recipient_email: Optional[str] = None,
        category: Optional[str] = None,
        sender_id: Optional[str] = None,
    ) -> List[Echo]:
        """
        Get echoes received by a user (inbox view).
        Only returns RELEASED echoes.
        Prefers user_id matching (more reliable), falls back to email matching.

        Args:
            user_id: Logged-in user's Cognito sub (preferred)
            recipient_email: Logged-in user's email (fallback)
            category: Filter by category
            sender_id: Filter by sender

        Returns:
            List of received echoes
        """
        try:
            recipient_ids = []

            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                recipients_table = await dynamodb.Table(self.recipients_table)

                # Strategy 1: Query by recipient_user_id (preferred, more reliable)
                if user_id:
                    try:
                        recipient_response = await recipients_table.query(
                            IndexName="recipient-user-id-index",
                            KeyConditionExpression="recipient_user_id = :user_id",
                            ExpressionAttributeValues={":user_id": user_id},
                        )
                        recipient_ids = [
                            item["recipient_id"]
                            for item in recipient_response.get("Items", [])
                        ]
                        if recipient_ids:
                            logger.info(
                                f"Found {len(recipient_ids)} recipients by user_id: {user_id}"
                            )
                    except Exception as e:
                        logger.warning(f"Could not query by recipient_user_id: {e}")

                # Strategy 2: Fallback to email query if no results from user_id
                if not recipient_ids and recipient_email:
                    recipient_response = await recipients_table.query(
                        IndexName="email-index",
                        KeyConditionExpression="email = :email",
                        ExpressionAttributeValues={":email": recipient_email.lower()},
                    )
                    recipient_ids = [
                        item["recipient_id"]
                        for item in recipient_response.get("Items", [])
                    ]
                    if recipient_ids:
                        logger.info(
                            f"Found {len(recipient_ids)} recipients by email: {recipient_email}"
                        )

                if not recipient_ids:
                    return []

                # Query echoes for these recipients with RELEASED status
                echoes_table = await dynamodb.Table(self.echoes_table)
                echoes = []

                for rid in recipient_ids:
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

                    for item in response.get("Items", []):
                        echo = Echo.from_dynamodb_item(item)

                        # Apply additional filters
                        if category and echo.category != category:
                            continue
                        if sender_id and echo.user_id != sender_id:
                            continue

                        echoes.append(echo)

                return echoes

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

            # Prevent updates to locked/released echoes (metadata only)
            if echo.status != EchoStatus.DRAFT:
                logger.warning(f"Attempted to update non-draft echo {echo_id}")
                raise InternalServerError("Cannot update locked or released echo")

            # Apply updates
            if "title" in data:
                echo.title = data["title"]
            if "category" in data:
                echo.category = data["category"]
            if "content" in data:
                echo.content = data["content"]
            if "media_url" in data:
                echo.media_url = data["media_url"]
            if "recipient_id" in data:
                echo.recipient_id = data["recipient_id"]

            echo.updated_at = _current_timestamp()

            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.echoes_table)
                await table.put_item(Item=echo.to_dynamodb_item())

            logger.info(f"Updated echo {echo_id}")
            return echo

        except (NotFoundError, InternalServerError):
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

            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
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
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
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

                await email_service.send_echo_notification(
                    recipient_email=recipient.email,
                    recipient_name=recipient.name,
                    sender_name=user_id,  # Caller's display name not available here;
                    # use user_id as fallback — routes layer can enrich if desired
                    echo_title=echo.title,
                    echo_category=echo.category,
                    echo_type=echo.echo_type.value,
                    is_registered=is_registered,
                )
                logger.info(
                    f"Sent echo notification for {echo_id} (registered={is_registered})"
                )
        except Exception as e:
            logger.warning(f"Failed to send echo notification for echo {echo_id}: {e}")

        return echo

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
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
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
    ) -> Dict[str, Any]:
        """
        Generate S3 presigned URL for direct upload.

        Args:
            user_id: User ID
            file_type: MIME type (e.g., "audio/mp4", "video/mp4")
            echo_id: Optional echo ID (if updating existing)

        Returns:
            Dict with 'upload_url' and 'key'
        """
        try:
            # Determine file extension from type
            extension = "mp4"  # Default
            if "audio" in file_type:
                extension = "m4a"
            elif "video" in file_type:
                extension = "mp4"

            # Generate unique key
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            key = f"echoes/{user_id}/{echo_id or 'new'}_{timestamp}.{extension}"

            async with self.session.client("s3", region_name=self.region) as s3:
                presigned_url = await s3.generate_presigned_url(
                    "put_object",
                    Params={
                        "Bucket": self.s3_bucket,
                        "Key": key,
                        "ContentType": file_type,
                    },
                    ExpiresIn=self.presigned_url_expiry,
                )

            # Construct the permanent media URL
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

    async def _sign_media_url(self, echo: Echo) -> Echo:
        """Generate presigned GET URL for secure media playback."""
        if echo.media_url and "amazonaws.com" in echo.media_url:
            try:
                # Extract key from URL
                # Format: https://{bucket}.s3.{region}.amazonaws.com/{key}
                key = echo.media_url.split("amazonaws.com/")[-1]

                async with self.session.client("s3", region_name=self.region) as s3:
                    presigned_url = await s3.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": self.s3_bucket, "Key": key},
                        ExpiresIn=3600,  # 1 hour
                    )
                    echo.media_url = presigned_url
            except Exception as e:
                logger.error(f"Failed to sign media URL for echo {echo.echo_id}: {e}")
                # Keep original URL on error
        return echo

    # ========================================
    # RECIPIENT CRUD OPERATIONS
    # ========================================

    async def create_recipient(self, user_id: str, data: Dict[str, Any]) -> Recipient:
        """Create a new recipient."""
        try:
            email = data.get("email", "").strip().lower()

            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
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

    async def get_user_recipients(self, user_id: str) -> List[Recipient]:
        """Get all active recipients for a user (excluding soft-deleted)."""
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.recipients_table)

                response = await table.query(
                    IndexName="user-recipients-index",
                    KeyConditionExpression="user_id = :user_id",
                    ExpressionAttributeValues={":user_id": user_id},
                )

                recipients = []
                for item in response.get("Items", []):
                    recipient = Recipient.from_dynamodb_item(item)
                    if recipient.deleted_at is None:
                        recipients.append(recipient)

                return recipients

        except ClientError as e:
            logger.error(f"Error getting recipients: {e}")
            raise InternalServerError(f"Failed to get recipients: {str(e)}")

    async def get_recipient(
        self, recipient_id: str, user_id: str
    ) -> Optional[Recipient]:
        """Get a specific recipient by ID."""
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
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
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
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

            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
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
                )

                await table.put_item(Item=guardian.to_dynamodb_item())

            logger.info(f"Created guardian {guardian.guardian_id} for user {user_id}")
            return guardian

        except ValidationError:
            raise
        except Exception as e:
            logger.error(f"Error creating guardian: {e}")
            raise InternalServerError(f"Failed to create guardian: {str(e)}")

    async def get_user_guardians(self, user_id: str) -> List[Guardian]:
        """Get all active guardians for a user."""
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.guardians_table)

                response = await table.query(
                    IndexName="user-guardians-index",
                    KeyConditionExpression="user_id = :user_id",
                    ExpressionAttributeValues={":user_id": user_id},
                )

                guardians = []
                for item in response.get("Items", []):
                    guardian = Guardian.from_dynamodb_item(item)
                    if guardian.deleted_at is None:
                        guardians.append(guardian)

                return guardians

        except ClientError as e:
            logger.error(f"Error getting guardians: {e}")
            raise InternalServerError(f"Failed to get guardians: {str(e)}")

    async def get_guardian(self, guardian_id: str, user_id: str) -> Guardian:
        """Get a specific guardian by ID."""
        try:
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
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
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
                table = await dynamodb.Table(self.guardians_table)

                response = await table.get_item(Key={"guardian_id": guardian_id})
                if "Item" not in response:
                    raise NotFoundError(f"Guardian {guardian_id} not found")

                guardian = Guardian.from_dynamodb_item(response["Item"])
                if guardian.user_id != user_id:
                    raise NotFoundError(f"Guardian {guardian_id} not found")

                # Apply permission updates
                scope = GuardianScope(data["scope"]) if "scope" in data else None
                trigger = (
                    GuardianTrigger(data["trigger"]) if "trigger" in data else None
                )

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
            async with self.session.resource(
                "dynamodb", **self._get_dynamodb_kwargs()
            ) as dynamodb:
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
