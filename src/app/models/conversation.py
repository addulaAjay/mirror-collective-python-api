"""
Conversation models for persistent chat history management
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4


@dataclass
class ConversationMessage:
    """Individual message in a conversation"""
    
    message_id: str
    conversation_id: str
    role: Literal["system", "user", "assistant"]
    content: str
    timestamp: str
    token_count: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    
    def __post_init__(self):
        if not self.message_id:
            self.message_id = str(uuid4())
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format"""
        item = asdict(self)
        # Remove None values to keep DynamoDB items clean
        return {k: v for k, v in item.items() if v is not None}
    
    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "ConversationMessage":
        """Create ConversationMessage from DynamoDB item"""
        return cls(
            message_id=item["message_id"],
            conversation_id=item["conversation_id"],
            role=item["role"],
            content=item["content"],
            timestamp=item["timestamp"],
            token_count=item.get("token_count"),
            metadata=item.get("metadata")
        )


@dataclass
class Conversation:
    """Conversation metadata and management"""
    
    conversation_id: str
    user_id: str
    title: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    message_count: int = 0
    total_tokens: int = 0
    is_archived: bool = False
    last_message_at: Optional[str] = None
    tags: Optional[List[str]] = None
    
    def __post_init__(self):
        if not self.conversation_id:
            self.conversation_id = str(uuid4())
        
        current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if not self.created_at:
            self.created_at = current_time
        if not self.updated_at:
            self.updated_at = current_time
        if not self.last_message_at:
            self.last_message_at = current_time
    
    def to_dynamodb_item(self) -> Dict[str, Any]:
        """Convert to DynamoDB item format"""
        item = asdict(self)
        # Remove None values and empty lists
        return {k: v for k, v in item.items() if v is not None and v != []}
    
    @classmethod
    def from_dynamodb_item(cls, item: Dict[str, Any]) -> "Conversation":
        """Create Conversation from DynamoDB item"""
        return cls(
            conversation_id=item["conversation_id"],
            user_id=item["user_id"],
            title=item.get("title"),
            created_at=item.get("created_at"),
            updated_at=item.get("updated_at"),
            message_count=item.get("message_count", 0),
            total_tokens=item.get("total_tokens", 0),
            is_archived=item.get("is_archived", False),
            last_message_at=item.get("last_message_at"),
            tags=item.get("tags", [])
        )
    
    def generate_title_from_content(self, first_message: str) -> str:
        """Generate a conversation title from the first message"""
        # Clean and truncate the first message for title
        title = first_message.strip()
        # Remove line breaks and extra spaces
        title = " ".join(title.split())
        # Truncate to reasonable length
        if len(title) > 60:
            title = title[:57] + "..."
        return title or "New Conversation"
    
    def update_activity(self, message_content: str, token_count: int = 0):
        """Update conversation activity metadata"""
        current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.updated_at = current_time
        self.last_message_at = current_time
        self.message_count += 1
        self.total_tokens += token_count
        
        # Auto-generate title from first user message if not set
        if not self.title and self.message_count == 1:
            self.title = self.generate_title_from_content(message_content)


@dataclass 
class ConversationSummary:
    """Lightweight conversation summary for listing"""
    
    conversation_id: str
    title: str
    last_message_at: Optional[str]  # Can be None for new conversations
    message_count: int
    is_archived: bool = False
    
    @classmethod
    def from_conversation(cls, conversation: Conversation) -> "ConversationSummary":
        """Create summary from full conversation object"""
        return cls(
            conversation_id=conversation.conversation_id,
            title=conversation.title or "Untitled Conversation",
            last_message_at=conversation.last_message_at or conversation.created_at,
            message_count=conversation.message_count,
            is_archived=conversation.is_archived
        )
