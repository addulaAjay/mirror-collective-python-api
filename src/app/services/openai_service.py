"""
OpenAI service for AI chat completions and conversation management
"""

import logging
import os
from typing import Dict, List, cast

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam

from ..core.exceptions import InternalServerError

logger = logging.getLogger(__name__)


class ChatMessage:
    """
    Represents a single message in a conversation with role and content
    """

    def __init__(self, role: str, content: str):
        if role not in ["system", "user", "assistant"]:
            raise ValueError(
                f"Invalid message role: {role}. Must be 'system', 'user', or 'assistant'"
            )
        self.role = role
        self.content = content

    def to_dict(self) -> Dict[str, str]:
        """Convert message to dictionary format for OpenAI API"""
        return {"role": self.role, "content": self.content}


class IMirrorChatRepository:
    """
    Abstract interface for mirror chat service implementations
    Defines the contract for AI conversation services
    """

    def send(self, messages: List[ChatMessage]) -> str:
        """
        Send conversation messages and return AI-generated response

        Args:
            messages: List of conversation messages

        Returns:
            str: AI response content
        """
        raise NotImplementedError(
            "send method must be implemented by concrete implementations"
        )


class OpenAIService(IMirrorChatRepository):
    """
    Service for generating AI responses using OpenAI's chat completion API
    Implements the mirror chat repository interface
    OPTIMIZED: Added async support and better error handling
    """

    def __init__(self):
        """Initialize OpenAI client"""
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")

        self.client = OpenAI(api_key=api_key)
        # Optimized model selection - gpt-4o-mini is faster and cheaper for most chat tasks
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.7"))
        self.max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "1000"))
        
        logger.info(f"OpenAI service initialized with model: {self.model}")

    def send(self, messages: List[ChatMessage]) -> str:
        """
        Generate AI response from conversation messages using OpenAI's chat completion

        Args:
            messages: List of conversation messages including system prompt and history

        Returns:
            str: AI-generated response content

        Raises:
            InternalServerError: If OpenAI API call fails
        """
        try:
            # Convert internal message format to OpenAI API format
            openai_messages: List[ChatCompletionMessageParam] = [
                cast(ChatCompletionMessageParam, msg.to_dict()) for msg in messages
            ]

            logger.debug(
                f"Generating AI response from {len(openai_messages)} conversation messages using {self.model}"
            )

            # Call OpenAI chat completion API with optimized settings
            response = self.client.chat.completions.create(
                model=self.model,
                messages=openai_messages,
                temperature=self.temperature,  # Configurable creativity
                max_tokens=self.max_tokens,    # Configurable response length
                stream=False,  # Disable streaming for now (could be enabled for real-time responses)
            )

            # Extract and validate response content
            reply = response.choices[0].message.content or ""

            logger.debug(f"AI response generated successfully: {len(reply)} characters")

            return reply

        except Exception as e:
            logger.error(f"OpenAI API error: {str(e)}")
            raise InternalServerError(f"Chat service unavailable: {str(e)}")

    async def send_async(self, messages: List[ChatMessage]) -> str:
        """
        Async version of send method for better performance
        
        Args:
            messages: List of conversation messages including system prompt and history

        Returns:
            str: AI-generated response content
        """
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.send, messages)
