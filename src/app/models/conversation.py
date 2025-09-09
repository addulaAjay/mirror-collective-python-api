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

    # Optional MirrorGPT analysis fields
    user_id: Optional[str] = None
    session_id: Optional[str] = None

    # 5-Signal Analysis Data
    signal_1_emotional_resonance: Optional[Dict[str, Any]] = None
    signal_2_symbolic_language: Optional[Dict[str, Any]] = None
    signal_3_archetype_blend: Optional[Dict[str, Any]] = None
    signal_4_narrative_position: Optional[Dict[str, Any]] = None
    signal_5_motif_loops: Optional[Dict[str, Any]] = None

    # Analysis metadata
    confidence_scores: Optional[Dict[str, Any]] = None
    change_detection: Optional[Dict[str, Any]] = None
    mirror_moment_triggered: Optional[bool] = None
    suggested_practice: Optional[str] = None
    archetype_context: Optional[str] = None
    analysis_version: Optional[str] = None

    def __post_init__(self):
        if not self.message_id:
            self.message_id = str(uuid4())
        if not self.timestamp:
            self.timestamp = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            )

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
            metadata=item.get("metadata"),
            # MirrorGPT analysis fields (optional)
            user_id=item.get("user_id"),
            session_id=item.get("session_id"),
            signal_1_emotional_resonance=item.get("signal_1_emotional_resonance"),
            signal_2_symbolic_language=item.get("signal_2_symbolic_language"),
            signal_3_archetype_blend=item.get("signal_3_archetype_blend"),
            signal_4_narrative_position=item.get("signal_4_narrative_position"),
            signal_5_motif_loops=item.get("signal_5_motif_loops"),
            confidence_scores=item.get("confidence_scores"),
            change_detection=item.get("change_detection"),
            mirror_moment_triggered=item.get("mirror_moment_triggered"),
            suggested_practice=item.get("suggested_practice"),
            archetype_context=item.get("archetype_context"),
            analysis_version=item.get("analysis_version"),
        )

    def has_mirrorgpt_analysis(self) -> bool:
        """Check if this message has MirrorGPT analysis data"""
        return (
            self.signal_1_emotional_resonance is not None
            or self.signal_3_archetype_blend is not None
        )

    def get_analysis_data(self) -> Dict[str, Any]:
        """Extract just the MirrorGPT analysis data as a dictionary"""
        return {
            "signal_1_emotional_resonance": self.signal_1_emotional_resonance,
            "signal_2_symbolic_language": self.signal_2_symbolic_language,
            "signal_3_archetype_blend": self.signal_3_archetype_blend,
            "signal_4_narrative_position": self.signal_4_narrative_position,
            "signal_5_motif_loops": self.signal_5_motif_loops,
            "confidence_scores": self.confidence_scores,
            "change_detection": self.change_detection,
            "mirror_moment_triggered": self.mirror_moment_triggered,
            "suggested_practice": self.suggested_practice,
            "archetype_context": self.archetype_context,
            "analysis_version": self.analysis_version,
        }

    def add_mirrorgpt_analysis(
        self,
        user_id: str,
        session_id: str,
        analysis_result: Dict[str, Any],
        confidence_scores: Dict[str, Any],
        change_analysis: Optional[Dict[str, Any]] = None,
        suggested_practice: Optional[str] = None,
    ):
        """Add MirrorGPT analysis data to this message"""
        self.user_id = user_id
        self.session_id = session_id

        # 5-Signal Analysis
        self.signal_1_emotional_resonance = analysis_result.get(
            "signal_1_emotional_resonance"
        )
        self.signal_2_symbolic_language = analysis_result.get(
            "signal_2_symbolic_language"
        )
        self.signal_3_archetype_blend = analysis_result.get("signal_3_archetype_blend")
        self.signal_4_narrative_position = analysis_result.get(
            "signal_4_narrative_position"
        )
        self.signal_5_motif_loops = analysis_result.get("signal_5_motif_loops")

        # Analysis metadata
        self.confidence_scores = confidence_scores
        self.change_detection = change_analysis
        self.mirror_moment_triggered = (
            change_analysis.get("mirror_moment_triggered", False)
            if change_analysis
            else False
        )
        self.suggested_practice = suggested_practice
        self.archetype_context = analysis_result.get(
            "signal_3_archetype_blend", {}
        ).get("primary")
        self.analysis_version = "1.0"


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
            tags=item.get("tags", []),
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
            is_archived=conversation.is_archived,
        )
