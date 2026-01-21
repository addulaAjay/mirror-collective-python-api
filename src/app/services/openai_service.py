"""
OpenAI service for AI chat completions and conversation management
"""

import logging
import os
from typing import AsyncGenerator, Dict, List, cast

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
            msg = (
                f"Invalid message role: {role}. "
                f"Must be 'system', 'user', or 'assistant'"
            )
            raise ValueError(msg)
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

    def send_stream(self, messages: List[ChatMessage]) -> AsyncGenerator[str, None]:
        """
        Send conversation messages and return streaming AI-generated response

        Args:
            messages: List of conversation messages

        Returns:
            AsyncGenerator[str, None]: Streaming AI response chunks
        """
        raise NotImplementedError(
            "send_stream method must be implemented by concrete implementations"
        )

    async def send_async(self, messages: List[ChatMessage]) -> str:
        """
        Send conversation messages asynchronously and return AI-generated response

        Args:
            messages: List of conversation messages

        Returns:
            str: AI response content
        """
        raise NotImplementedError(
            "send_async method must be implemented by concrete implementations"
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
        # High-performance model selection - gpt-4o for better quality
        # and faster response
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o")
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
                f"Generating AI response from {len(openai_messages)} "
                f"conversations using {self.model}"
            )

            # Call OpenAI chat completion API with optimized settings (non-streaming)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=openai_messages,
                temperature=self.temperature,  # Configurable creativity
                max_tokens=self.max_tokens,  # Configurable response length
                stream=False,
            )

            # Extract and validate response content
            reply = response.choices[0].message.content or ""

            logger.debug(f"AI response generated successfully: {len(reply)} characters")

            return reply

        except Exception as e:
            logger.error(f"OpenAI API error: {str(e)}")
            raise InternalServerError(f"Chat service unavailable: {str(e)}")

    async def send_stream(
        self, messages: List[ChatMessage]
    ) -> AsyncGenerator[str, None]:
        """
        Generate streaming AI response from messages using OpenAI's
        chat completion

        Args:
            messages: List of conversation messages including system prompt and history

        Yields:
            str: AI-generated response chunks

        Raises:
            InternalServerError: If OpenAI API call fails
        """
        try:
            # Convert internal message format to OpenAI API format
            openai_messages: List[ChatCompletionMessageParam] = [
                cast(ChatCompletionMessageParam, msg.to_dict()) for msg in messages
            ]

            logger.debug(
                f"Generating streaming AI response from "
                f"{len(openai_messages)} messages using {self.model}"
            )

            # Call OpenAI chat completion API with streaming enabled
            response = self.client.chat.completions.create(
                model=self.model,
                messages=openai_messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                stream=True,
            )

            # Stream response chunks
            for chunk in response:
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

            logger.debug("Streaming AI response completed successfully")

        except Exception as e:
            logger.error(f"OpenAI API streaming error: {str(e)}")
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
