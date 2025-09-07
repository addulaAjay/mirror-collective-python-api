"""
User profile models for DynamoDB persistence
"""
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from enum import Enum


class UserStatus(Enum):
    """User account status synchronized with Cognito"""
    UNCONFIRMED = "UNCONFIRMED"
    CONFIRMED = "CONFIRMED" 
    ARCHIVED = "ARCHIVED"
    COMPROMISED = "COMPROMISED"
    UNKNOWN = "UNKNOWN"
    RESET_REQUIRED = "RESET_REQUIRED"
    FORCE_CHANGE_PASSWORD = "FORCE_CHANGE_PASSWORD"


@dataclass
class UserProfile:
    """
    User profile model that syncs with Cognito and stores additional app data
    """
    # Primary identifiers
    user_id: str  # Cognito sub (UUID)
    email: str
    
    # Basic profile information
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    display_name: Optional[str] = None
    
    # Account status (synced with Cognito)
    status: UserStatus = UserStatus.UNCONFIRMED
    email_verified: bool = False
    
    # Application-specific data
    preferences: Dict[str, Any] = None
    subscription_tier: str = "free"
    conversation_count: int = 0
    
    # Timestamps
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_login_at: Optional[str] = None
    
    # Cognito sync metadata
    cognito_username: Optional[str] = None
    last_cognito_sync: Optional[str] = None
    
    def __post_init__(self):
        """Set defaults after initialization"""
        if self.preferences is None:
            self.preferences = {}
        
        current_time = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        if self.created_at is None:
            self.created_at = current_time
        self.updated_at = current_time
    
    @property
    def full_name(self) -> str:
        """Get user's full name with fallbacks"""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}".strip()
        elif self.display_name:
            return self.display_name
        elif self.first_name:
            return self.first_name
        else:
            return self.email.split('@')[0] if self.email else "User"
    
    @property
    def chat_name(self) -> str:
        """Get the name to use in chat conversations"""
        return self.display_name or self.first_name or self.email.split('@')[0] if self.email else None
    
    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format"""
        item = asdict(self)
        item['status'] = self.status.value  # Convert enum to string
        return {k: v for k, v in item.items() if v is not None}
    
    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> 'UserProfile':
        """Create UserProfile from DynamoDB item"""
        # Convert status string back to enum
        if 'status' in item:
            try:
                item['status'] = UserStatus(item['status'])
            except ValueError:
                item['status'] = UserStatus.UNKNOWN
        
        return cls(**item)
    
    @classmethod
    def from_cognito_user(cls, cognito_user_data: Dict[str, Any], user_id: str) -> 'UserProfile':
        """
        Create UserProfile from Cognito user data
        
        Args:
            cognito_user_data: Data from Cognito GetUser or AdminGetUser
            user_id: The user's Cognito sub (UUID)
        """
        # Extract attributes from Cognito format
        attributes = {}
        if 'UserAttributes' in cognito_user_data:
            for attr in cognito_user_data['UserAttributes']:
                attributes[attr['Name']] = attr['Value']
        elif 'Attributes' in cognito_user_data:
            for attr in cognito_user_data['Attributes']:
                attributes[attr['Name']] = attr['Value']
        
        # Map Cognito status to our enum
        cognito_status = cognito_user_data.get('UserStatus', 'UNKNOWN')
        try:
            status = UserStatus(cognito_status)
        except ValueError:
            status = UserStatus.UNKNOWN
        
        return cls(
            user_id=user_id,
            email=attributes.get('email', ''),
            first_name=attributes.get('given_name'),
            last_name=attributes.get('family_name'),
            status=status,
            email_verified=attributes.get('email_verified', 'false').lower() == 'true',
            cognito_username=cognito_user_data.get('Username'),
            last_cognito_sync=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        )
    
    def update_from_cognito(self, cognito_user_data: Dict[str, Any]) -> None:
        """Update profile with latest Cognito data"""
        # Extract attributes
        attributes = {}
        if 'UserAttributes' in cognito_user_data:
            for attr in cognito_user_data['UserAttributes']:
                attributes[attr['Name']] = attr['Value']
        
        # Update fields that come from Cognito
        self.email = attributes.get('email', self.email)
        if attributes.get('given_name'):
            self.first_name = attributes['given_name']
        if attributes.get('family_name'):
            self.last_name = attributes['family_name']
        
        # Update status
        cognito_status = cognito_user_data.get('UserStatus')
        if cognito_status:
            try:
                self.status = UserStatus(cognito_status)
            except ValueError:
                self.status = UserStatus.UNKNOWN
        
        self.email_verified = attributes.get('email_verified', 'false').lower() == 'true'
        self.cognito_username = cognito_user_data.get('Username', self.cognito_username)
        self.last_cognito_sync = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        self.updated_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


@dataclass
class UserActivity:
    """Track user activity for analytics and usage patterns"""
    user_id: str
    activity_date: str  # YYYY-MM-DD format for partitioning
    
    # Activity counters
    chat_messages: int = 0
    login_count: int = 0
    
    # Last activity timestamps
    last_chat_at: Optional[str] = None
    last_login_at: Optional[str] = None
    
    # Session data
    session_duration_minutes: int = 0
    
    def __post_init__(self):
        """Set current date if not provided"""
        if not self.activity_date:
            self.activity_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format"""
        return {k: v for k, v in asdict(self).items() if v is not None}