"""
Mirror chat use case - orchestrates personalized AI conversations
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from ..services.openai_service import ChatMessage, IMirrorChatRepository

logger = logging.getLogger(__name__)


class MirrorChatRequest:
    """
    Encapsulates a mirror chat request with message, conversation history, and user context
    """

    def __init__(
        self,
        message: str,
        conversation_history: Optional[List[ChatMessage]] = None,
        user_name: Optional[str] = None,
    ):
        self.message = message
        self.conversation_history = conversation_history or []
        self.user_name = user_name


class MirrorChatResponse:
    """
    Represents the response from a mirror chat interaction
    """

    def __init__(self, reply: str, timestamp: str):
        self.reply = reply
        self.timestamp = timestamp

    def to_dict(self):
        """Convert response to dictionary format for API serialization"""
        return {"reply": self.reply, "timestamp": self.timestamp}


class MirrorChatUseCase:
    """
    Business logic for handling mirror chat conversations with personalized AI guidance
    """

    def __init__(self, chat_service: IMirrorChatRepository):
        self.chat_service = chat_service

    async def execute(self, request: MirrorChatRequest) -> MirrorChatResponse:
        """
        Process a mirror chat request and generate a personalized AI response

        Args:
            request: The chat request containing message, history, and user context

        Returns:
            MirrorChatResponse: AI-generated response with timestamp
        """
        # Create system prompt with empathetic guidance persona
        system_content = "You are a deeply empathetic, spiritually-aware guide. Respond with clarity, emotional resonance, and gentle encouragement for self-reflection."

        # Personalize the system prompt with user's name if provided
        if request.user_name:
            system_content += f" The user you are speaking with is named {request.user_name}. Use their name naturally in your responses to create a more personal connection."

        system_prompt = ChatMessage(role="system", content=system_content)

        # Construct complete conversation context for AI
        messages: List[ChatMessage] = [
            system_prompt,
            *request.conversation_history,  # Include previous conversation turns
            ChatMessage(
                role="user", content=request.message
            ),  # Add current user message
        ]

        logger.debug(
            f"Processing mirror chat with {len(messages)} messages for user: {request.user_name or 'anonymous'}"
        )

        # Generate AI response using the chat service
        reply = self.chat_service.send(messages)

        # Return structured response with UTC timestamp
        return MirrorChatResponse(
            reply=reply,
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
