"""
Echo Vault models for DynamoDB persistence.
Includes Echo, Recipient, and Guardian entities.
"""

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4


class EchoType(Enum):
    """Type of echo content"""

    TEXT = "TEXT"
    AUDIO = "AUDIO"
    VIDEO = "VIDEO"


class AttachmentType(Enum):
    """Type of a single media attachment on an echo."""

    IMAGE = "IMAGE"  # photo (jpg/png) — gallery or file
    VIDEO = "VIDEO"  # recorded or picked video
    AUDIO = "AUDIO"  # voice recording / audio file
    FILE = "FILE"  # other documents (e.g. pdf)


class EchoStatus(Enum):
    """Status of an echo in the vault"""

    DRAFT = "DRAFT"  # Still being edited
    LOCKED = "LOCKED"  # Finalized, waiting for trigger
    RELEASED = "RELEASED"  # Released to recipient(s)


class GuardianScope(Enum):
    """Access scope for guardians"""

    ALL = "ALL"  # Access to all echoes
    SELECTED = "SELECTED"  # Access to specific echoes only


class GuardianTrigger(Enum):
    """How echoes are released by guardian"""

    MANUAL = "MANUAL"  # Guardian manually releases
    AUTOMATIC = "AUTOMATIC"  # Automatic after conditions met


def _generate_id() -> str:
    """Generate a unique ID"""
    return str(uuid4())


def _current_timestamp() -> str:
    """Get current UTC timestamp in ISO format"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class Attachment:
    """A single media attachment on an echo.

    An echo carries a text ``content`` plus zero or more attachments
    (photo/video/voice/file). ``media_url`` holds the canonical (non-presigned)
    S3 URL — the service signs it on read. ``duration`` is a display string
    like ``"2:32"`` for audio/video.
    """

    attachment_id: str = field(default_factory=_generate_id)
    type: AttachmentType = AttachmentType.FILE
    media_url: str = ""  # canonical S3 URL; signed on read
    # Web-playable H.264/AAC MP4 rendition (set by the MediaConvert pipeline
    # once transcoding of an iOS .mov/HEVC video completes). The share viewer
    # plays this so video works in every browser, not just Safari.
    playable_url: Optional[str] = None
    thumb_url: Optional[str] = None  # poster/thumbnail for video/image
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    duration: Optional[str] = None  # "2:32" for audio/video
    filename: Optional[str] = None
    created_at: str = field(default_factory=_current_timestamp)

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Serialize to a DynamoDB-safe map (enum -> str, drop Nones)."""
        item = asdict(self)
        item["type"] = self.type.value
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "Attachment":
        """Build from a DynamoDB map, tolerant of unknown/extra keys."""
        data = dict(item)
        if "type" in data:
            try:
                data["type"] = AttachmentType(data["type"])
            except ValueError:
                data["type"] = AttachmentType.FILE
        # DynamoDB returns numbers as Decimal — coerce size back to int.
        if data.get("size_bytes") is not None:
            try:
                data["size_bytes"] = int(data["size_bytes"])
            except (TypeError, ValueError):
                data["size_bytes"] = None
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class Echo:
    """
    Echo model representing a vault item (legacy message).
    User (1) -> Echoes (Many)
    """

    # Primary identifiers
    echo_id: str = field(default_factory=_generate_id)
    user_id: str = ""  # Owner of the echo (creator)

    # Content metadata
    title: str = ""
    category: str = ""
    echo_type: EchoType = EchoType.TEXT

    # Media storage (S3 URL for audio/video, inline for text)
    media_url: Optional[str] = None
    # Optional video poster frame — JPEG extracted client-side at upload
    # time. Used as the thumbnail in list views so video cards don't
    # render as a black rectangle until the player initializes. Set by
    # POST /echoes/{id}/attach-poster after the video upload succeeds.
    poster_url: Optional[str] = None
    content: Optional[str] = None  # For text type only

    # Media attachments (photo / video / voice / file). An echo now carries a
    # text message PLUS zero or more attachments. media_url/poster_url above are
    # retained for back-compat (they mirror the primary audio/video attachment).
    attachments: List[Attachment] = field(default_factory=list)

    # Status and delivery
    status: EchoStatus = EchoStatus.DRAFT
    recipient_id: Optional[str] = None  # References Recipient

    # Optional cover note shown alongside the echo in the recipient's inbox.
    # Captured on the "Letter to Recipient" field in ChooseRecipientScreen
    # during create/edit. Distinct from `content` (which is the echo body for
    # TEXT echoes / unused for AUDIO/VIDEO).
    letter_to_recipient: Optional[str] = None

    # Lock/Release scheduling
    lock_date: Optional[str] = None  # When echo was locked
    release_date: Optional[str] = None  # When to auto-release (optional)
    unlock_on_death: bool = False  # If true, guardian releases upon creator's death

    # Guardian linkage
    guardian_id: Optional[str] = None  # Guardian who can manage release

    # Timestamps
    created_at: str = field(default_factory=_current_timestamp)
    updated_at: str = field(default_factory=_current_timestamp)

    # Soft delete
    deleted_at: Optional[str] = None

    # Enriched data (not persisted in DynamoDB)
    recipient: Optional[Dict[str, Any]] = None

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format"""
        item = asdict(self)

        # Convert enums to strings
        item["echo_type"] = self.echo_type.value
        item["status"] = self.status.value

        # asdict() leaves nested AttachmentType enums unserialized — rebuild
        # the list via each attachment's own serializer (enum -> str, no Nones).
        item["attachments"] = [a.to_dynamodb_item() for a in self.attachments]

        # Filter out None values (empty attachments list is kept, not dropped)
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "Echo":
        """Create Echo from DynamoDB item"""
        item = dict(item)  # don't mutate the caller's dict

        # Convert enum strings back to enums
        if "echo_type" in item:
            try:
                item["echo_type"] = EchoType(item["echo_type"])
            except ValueError:
                item["echo_type"] = EchoType.TEXT

        if "status" in item:
            try:
                item["status"] = EchoStatus(item["status"])
            except ValueError:
                item["status"] = EchoStatus.DRAFT

        # Rehydrate attachments (older rows have no attachments key -> []).
        raw_attachments = item.get("attachments") or []
        item["attachments"] = [
            Attachment.from_dynamodb_item(a) for a in raw_attachments
        ]

        return cls(**item)

    def lock(self) -> None:
        """Lock the echo, preventing further edits"""
        self.status = EchoStatus.LOCKED
        self.lock_date = _current_timestamp()
        self.updated_at = _current_timestamp()

    def release(self) -> None:
        """Release the echo to recipient(s)"""
        self.status = EchoStatus.RELEASED
        self.updated_at = _current_timestamp()


@dataclass
class Recipient:
    """
    Recipient model representing a trusted contact who can receive echoes.
    User (1) -> Recipients (Many)
    """

    # Primary identifiers
    recipient_id: str = field(default_factory=_generate_id)
    user_id: str = ""  # Owner who added this recipient

    # Contact info
    name: str = ""
    email: str = ""
    recipient_user_id: Optional[str] = (
        None  # Cognito sub of the recipient (if they have an account)
    )

    # Optional metadata
    motif: Optional[str] = None  # Personal motif/symbol
    relationship: Optional[str] = None  # e.g., "Family", "Friend", "Work"
    profile_image_url: Optional[str] = None  # S3 URL of uploaded profile photo

    # Timestamps
    created_at: str = field(default_factory=_current_timestamp)
    updated_at: str = field(default_factory=_current_timestamp)

    # Soft delete
    deleted_at: Optional[str] = None

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format"""
        item = asdict(self)
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "Recipient":
        """Create Recipient from DynamoDB item"""
        return cls(**item)

    def soft_delete(self) -> None:
        """Soft delete the recipient"""
        self.deleted_at = _current_timestamp()
        self.updated_at = _current_timestamp()


@dataclass
class Guardian:
    """
    Guardian model representing a legacy contact who manages echo releases.
    User (1) -> Guardians (Many)
    """

    # Primary identifiers
    guardian_id: str = field(default_factory=_generate_id)
    user_id: str = ""  # Owner who added this guardian

    # Contact info
    name: str = ""
    email: str = ""

    # Optional profile photo
    profile_image_url: Optional[str] = None  # S3 URL of uploaded profile photo

    # Permissions
    scope: GuardianScope = GuardianScope.ALL
    trigger: GuardianTrigger = GuardianTrigger.MANUAL

    # Optional: Specific echoes/recipients this guardian can manage
    # Only relevant when scope == SELECTED
    allowed_echo_ids: List[str] = field(default_factory=list)
    allowed_recipient_ids: List[str] = field(default_factory=list)

    # Timestamps
    created_at: str = field(default_factory=_current_timestamp)
    updated_at: str = field(default_factory=_current_timestamp)

    # Soft delete
    deleted_at: Optional[str] = None

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format"""
        item = asdict(self)

        # Convert enums to strings
        item["scope"] = self.scope.value
        item["trigger"] = self.trigger.value

        # Filter out None values (but keep empty lists)
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "Guardian":
        """Create Guardian from DynamoDB item"""
        # Convert enum strings back to enums
        if "scope" in item:
            try:
                item["scope"] = GuardianScope(item["scope"])
            except ValueError:
                item["scope"] = GuardianScope.ALL

        if "trigger" in item:
            try:
                item["trigger"] = GuardianTrigger(item["trigger"])
            except ValueError:
                item["trigger"] = GuardianTrigger.MANUAL

        return cls(**item)

    def soft_delete(self) -> None:
        """Soft delete the guardian"""
        self.deleted_at = _current_timestamp()
        self.updated_at = _current_timestamp()

    def update_permissions(
        self,
        scope: Optional[GuardianScope] = None,
        trigger: Optional[GuardianTrigger] = None,
        allowed_echo_ids: Optional[List[str]] = None,
        allowed_recipient_ids: Optional[List[str]] = None,
    ) -> None:
        """Update guardian permissions"""
        if scope is not None:
            self.scope = scope
        if trigger is not None:
            self.trigger = trigger
        if allowed_echo_ids is not None:
            self.allowed_echo_ids = allowed_echo_ids
        if allowed_recipient_ids is not None:
            self.allowed_recipient_ids = allowed_recipient_ids
        self.updated_at = _current_timestamp()


# Request/Response DTOs for API layer
@dataclass
class CreateEchoRequest:
    """Request DTO for creating an echo"""

    title: str
    category: str
    echo_type: str  # TEXT, AUDIO, VIDEO
    recipient_id: Optional[str] = None
    content: Optional[str] = None  # For text type


@dataclass
class CreateRecipientRequest:
    """Request DTO for adding a recipient"""

    name: str
    email: str
    relationship: Optional[str] = None


@dataclass
class CreateGuardianRequest:
    """Request DTO for adding a guardian"""

    name: str
    email: str
    scope: str = "ALL"  # ALL, SELECTED
    trigger: str = "MANUAL"  # MANUAL, AUTOMATIC


@dataclass
class UpdateGuardianPermissionsRequest:
    """Request DTO for updating guardian permissions"""

    scope: Optional[str] = None
    trigger: Optional[str] = None
    allowed_echo_ids: Optional[List[str]] = None
    allowed_recipient_ids: Optional[List[str]] = None
