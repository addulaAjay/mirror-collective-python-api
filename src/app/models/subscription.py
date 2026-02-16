"""
Subscription models for in-app purchase management
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class SubscriptionStatus(Enum):
    """Subscription status lifecycle"""

    NONE = "none"
    TRIAL = "trial"
    TRIAL_EXPIRED = "trial_expired"
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    GRACE_PERIOD = "grace_period"
    REFUNDED = "refunded"


class SubscriptionType(Enum):
    """Type of subscription"""

    MIRROR_CORE = "core"  # Mirror Core plan
    STORAGE_ADD_ON = "storage"  # Echo Vault Storage add-on


class BillingPeriod(Enum):
    """Billing cycle period"""

    MONTHLY = "monthly"
    YEARLY = "yearly"


class Platform(Enum):
    """Platform where purchase was made"""

    IOS = "ios"
    ANDROID = "android"


@dataclass
class Subscription:
    """
    Subscription model for user subscriptions (iOS/Android IAP)
    """

    # Primary identifiers
    user_id: str  # Cognito sub (UUID)
    subscription_id: str  # Platform transaction ID (original_transaction_id for iOS, orderId for Android)

    # Subscription details
    product_id: str  # e.g. com.mirrorcollective.core.monthly
    subscription_type: SubscriptionType
    platform: Platform
    status: SubscriptionStatus

    # Billing information
    billing_period: BillingPeriod
    price_usd: float
    currency_code: str = "USD"

    # Trial management (for platform trials, not our in-app trial)
    trial_start_date: Optional[str] = None  # ISO 8601
    trial_end_date: Optional[str] = None  # ISO 8601
    is_in_trial: bool = False

    # Subscription lifecycle
    purchase_date: Optional[str] = None  # ISO 8601
    expiry_date: Optional[str] = None  # ISO 8601
    auto_renew_enabled: bool = True
    cancellation_date: Optional[str] = None  # ISO 8601

    # Receipt validation
    receipt_data: Optional[str] = (
        None  # Base64 receipt (iOS) or purchase token (Android)
    )
    original_transaction_id: Optional[str] = None  # iOS only
    latest_receipt_info: Optional[Dict[str, Any]] = None  # Full receipt details
    last_validation_date: Optional[str] = None  # ISO 8601
    validation_environment: str = "production"  # production | sandbox

    # Metadata
    created_at: Optional[str] = None  # ISO 8601
    updated_at: Optional[str] = None  # ISO 8601
    events: Optional[List[Dict[str, Any]]] = None  # Event history

    def __post_init__(self):
        """Set defaults after initialization"""
        if self.events is None:
            self.events = []

        current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if self.created_at is None:
            self.created_at = current_time
        self.updated_at = current_time

        # Convert enums to values if strings were passed
        if isinstance(self.subscription_type, str):
            self.subscription_type = SubscriptionType(self.subscription_type)
        if isinstance(self.platform, str):
            self.platform = Platform(self.platform)
        if isinstance(self.status, str):
            self.status = SubscriptionStatus(self.status)
        if isinstance(self.billing_period, str):
            self.billing_period = BillingPeriod(self.billing_period)

    def add_event(self, event_type: str, details: Optional[Dict[str, Any]] = None):
        """Add an event to the subscription history"""
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        event = {
            "event_type": event_type,
            "timestamp": timestamp,
            "details": details or {},
        }
        if self.events is None:
            self.events = []
        self.events.append(event)
        self.updated_at = timestamp

    def is_active(self) -> bool:
        """Check if subscription is currently active"""
        if self.status not in [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL]:
            return False

        if not self.expiry_date:
            return False

        expiry = datetime.fromisoformat(self.expiry_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return expiry > now

    def days_until_expiry(self) -> int:
        """Calculate days until subscription expires"""
        if not self.expiry_date:
            return 0

        expiry = datetime.fromisoformat(self.expiry_date.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = expiry - now
        return max(0, delta.days)

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format"""
        item = asdict(self)

        # Convert enums to strings
        item["subscription_type"] = self.subscription_type.value
        item["platform"] = self.platform.value
        item["status"] = self.status.value
        item["billing_period"] = self.billing_period.value

        # Filter out None values
        filtered_item = {k: v for k, v in item.items() if v is not None}

        return filtered_item

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "Subscription":
        """Create Subscription from DynamoDB item"""
        # Convert string values back to enums
        if "subscription_type" in item:
            item["subscription_type"] = SubscriptionType(item["subscription_type"])
        if "platform" in item:
            item["platform"] = Platform(item["platform"])
        if "status" in item:
            item["status"] = SubscriptionStatus(item["status"])
        if "billing_period" in item:
            item["billing_period"] = BillingPeriod(item["billing_period"])

        return cls(**item)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "subscription_id": self.subscription_id,
            "product_id": self.product_id,
            "subscription_type": self.subscription_type.value,
            "platform": self.platform.value,
            "status": self.status.value,
            "billing_period": self.billing_period.value,
            "price_usd": self.price_usd,
            "purchase_date": self.purchase_date,
            "expiry_date": self.expiry_date,
            "auto_renew_enabled": self.auto_renew_enabled,
            "is_active": self.is_active(),
            "days_until_expiry": self.days_until_expiry(),
        }


@dataclass
class SubscriptionEvent:
    """
    Audit log for subscription events
    """

    event_id: str  # UUID
    user_id: str
    subscription_id: str
    event_type: str  # purchased, trial_started, renewed, cancelled, expired, refunded
    timestamp: str  # ISO 8601
    platform: Platform
    receipt_data: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        """Convert platform to enum if string"""
        if isinstance(self.platform, str):
            self.platform = Platform(self.platform)

        if self.metadata is None:
            self.metadata = {}

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format"""
        item = asdict(self)
        item["platform"] = self.platform.value
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "SubscriptionEvent":
        """Create SubscriptionEvent from DynamoDB item"""
        if "platform" in item:
            item["platform"] = Platform(item["platform"])
        return cls(**item)
