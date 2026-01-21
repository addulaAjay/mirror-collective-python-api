"""
Device token models for DynamoDB persistence
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class DeviceToken:
    """
    Device token model for push notifications mapping
    """

    user_id: str  # Cognito sub (UUID) - Hash Key
    device_token: str  # FCM/APNs token - Range Key
    endpoint_arn: str  # AWS SNS Platform Endpoint ARN
    platform: str  # 'android' or 'ios'
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    is_active: bool = True

    def __post_init__(self):
        """Set defaults after initialization"""
        current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if self.created_at is None:
            self.created_at = current_time
        if self.updated_at is None:
            self.updated_at = current_time

    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format"""
        item = asdict(self)
        # Filter out None values
        return {k: v for k, v in item.items() if v is not None}

    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "DeviceToken":
        """Create DeviceToken from DynamoDB item"""
        return cls(**item)
