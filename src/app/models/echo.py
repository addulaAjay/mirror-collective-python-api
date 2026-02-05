"""
Echo Vault models for DynamoDB persistence.
Includes Echo, Recipient, and Guardian entities.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4


class EchoType(Enum):
    """Type of echo content"""

    TEXT = "TEXT"
    AUDIO = "AUDIO"
    VIDEO = "VIDEO"


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
    content: Optional[str] = None  # For text type only

    # Status and delivery
    status: EchoStatus = EchoStatus.DRAFT
    recipient_id: Optional[str] = None  # References Recipient

    # Lock/Release scheduling
    lock_date: Optional[str] = None  # When echo was locked
    release_date: Optional[str] = None  # When to auto-release (optional)

    # Guardian linkage
    guardian_id: Optional[str] = None  # Guardian who can manage release

    # Timestamps
    created_at: str = field(default_factory=_current_timestamp)
    updated_at: str = field(default_factory=_current_timestamp)

    # Soft delete
    deleted_at: Optional[str] = None

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format"""
        item = asdict(self)

        # Convert enums to strings
        item["echo_type"] = self.echo_type.value
        item["status"] = self.status.value

        # Filter out None values
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "Echo":
        """Create Echo from DynamoDB item"""
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

    # Optional metadata
    motif: Optional[str] = None  # Personal motif/symbol
    relationship: Optional[str] = None  # e.g., "Family", "Friend", "Work"

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
