"""
OpenAI service for AI chat completions and conversation management
"""

import asyncio
import logging
import os
from functools import lru_cache
from typing import Any, AsyncGenerator, Dict, List, Optional, cast

from openai import AsyncOpenAI, OpenAI
from openai.types.chat import ChatCompletionMessageParam

from ..core.exceptions import InternalServerError

logger = logging.getLogger(__name__)


# Module-level concurrency cap for in-process OpenAI calls.
#
# Why module-level (not instance-level)? Multiple OpenAIService instances may
# be constructed in the same Lambda container (DI, helpers, ancillary callers
# like the summarizer). We want ONE shared limit per process so a runaway
# request rate doesn't fan out and trip OpenAI's account rate limit.
#
# Why lazy-init? asyncio.Semaphore must be created inside a running event
# loop. Constructing it at import time would bind it to whichever loop
# happens to be current then — usually none.
_OPENAI_MAX_INFLIGHT = int(os.getenv("OPENAI_MAX_INFLIGHT", "16"))
_openai_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    """Lazily create the shared OpenAI concurrency semaphore.

    Lazy because asyncio.Semaphore needs a running event loop, and we don't
    necessarily have one at module import time.
    """
    global _openai_semaphore
    if _openai_semaphore is None:
        _openai_semaphore = asyncio.Semaphore(_OPENAI_MAX_INFLIGHT)
    return _openai_semaphore


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

    Holds BOTH a sync OpenAI client and an AsyncOpenAI client:
      - send / send_with_overrides: sync paths, kept for backward compat with
        any caller that still invokes them synchronously.
      - send_async / send_with_overrides_async / send_stream: native async
        paths that await the AsyncOpenAI client directly — they no longer
        burn a ThreadPoolExecutor worker for the full ~1-4s OpenAI call.

    All async paths share a module-level asyncio.Semaphore (`_get_semaphore`)
    so a high request rate can't fan out unbounded against OpenAI's API.
    """

    def __init__(self):
        """Initialize OpenAI sync + async clients."""
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")

        # timeout=20.0 keeps us well inside Lambda's 30s budget; the SDK
        # default of 600s would let a hung OpenAI request time the Lambda
        # out without the client ever cancelling. max_retries=1 means
        # one retry on transient errors (network/5xx), instead of the SDK
        # default of 2 which compounds inside the same Lambda invocation.
        self.client = OpenAI(api_key=api_key, timeout=20.0, max_retries=1)
        # AsyncOpenAI mirrors the sync client config exactly. Async paths use
        # this so the event loop is never blocked on a network round-trip
        # and we don't occupy a ThreadPoolExecutor worker for the full call
        # duration (previously: ~1-4s per call against the default 40-worker
        # pool — 10 concurrent chats would saturate the executor).
        self.async_client = AsyncOpenAI(api_key=api_key, timeout=20.0, max_retries=1)
        # gpt-4o-mini is the conversational default — the system prompt
        # asks for short, conversational replies that the mini model
        # handles well at ~5-10% of the cost of gpt-4o. Callers that need
        # higher reasoning (e.g. a "deep reflection" mode) should pass a
        # specific model via send_with_overrides instead of changing this
        # default.
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.temperature = float(os.getenv("OPENAI_TEMPERATURE", "0.7"))
        # 450 tokens ≈ 340 words — plenty for the 1-3 sentence replies the
        # system prompt requests. The previous 1000 default let responses
        # drift much longer than the conversational tone calls for and was
        # the dominant per-call cost driver.
        self.max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "450"))

        logger.info(f"OpenAI service initialized with model: {self.model}")

    def send_with_overrides(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Send messages with per-call model/temperature/max_tokens overrides.

        Used by ancillary callers (e.g. ConversationSummarizer) that need a
        cheaper or differently-tuned model than the main chat default. Does
        not mutate instance state. Streaming is intentionally not supported
        here — these calls are short-form completions.

        Kept synchronous for backward compatibility with any non-async caller.
        Prefer `send_with_overrides_async` for the request hot path.
        """
        try:
            openai_messages: List[ChatCompletionMessageParam] = [
                cast(ChatCompletionMessageParam, msg.to_dict()) for msg in messages
            ]

            response = self.client.chat.completions.create(
                model=model,
                messages=openai_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
            )

            return response.choices[0].message.content or ""

        except Exception as e:
            logger.error(f"OpenAI API error (overrides): {str(e)}")
            raise InternalServerError(f"Chat service unavailable: {str(e)}")

    async def send_with_overrides_async(
        self,
        messages: List[ChatMessage],
        model: str,
        temperature: float,
        max_tokens: int,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Async override-aware completion using the native AsyncOpenAI client.

        Awaits the OpenAI call directly — no run_in_executor, no thread pool
        occupancy. Bounded by the module-level concurrency semaphore so a
        burst of summarizer calls can't fan out unbounded.
        """
        try:
            openai_messages: List[ChatCompletionMessageParam] = [
                cast(ChatCompletionMessageParam, msg.to_dict()) for msg in messages
            ]

            create_kwargs: Dict[str, Any] = {
                "model": model,
                "messages": openai_messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
            # e.g. {"type": "json_object"} to force valid-JSON output. Requires
            # the word "json" somewhere in the prompt (OpenAI constraint).
            if response_format is not None:
                create_kwargs["response_format"] = response_format

            async with _get_semaphore():
                response = await self.async_client.chat.completions.create(
                    **create_kwargs
                )

            return response.choices[0].message.content or ""

        except Exception as e:
            logger.error(f"OpenAI API error (overrides async): {str(e)}")
            raise InternalServerError(f"Chat service unavailable: {str(e)}")

    def send(self, messages: List[ChatMessage]) -> str:
        """
        Generate AI response from conversation messages using OpenAI's chat completion

        Kept synchronous for backward compatibility with any non-async caller.
        Prefer `send_async` for the request hot path — it doesn't block the
        event loop or occupy a ThreadPoolExecutor worker.

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
        Generate streaming AI response from messages using AsyncOpenAI.

        Uses an async iterator (`async for chunk in stream`) so the event
        loop is never blocked while waiting for the next chunk — previously
        the sync iterator blocked the loop for the entire stream duration.

        Args:
            messages: List of conversation messages including system prompt
                and history

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

            # Acquire the semaphore ONLY around the initial create() call,
            # not the entire stream iteration. A streaming response can run
            # for several seconds while chunks trickle in; holding the
            # semaphore slot for that whole duration would collapse the
            # effective concurrency cap from N to ~N/(stream_duration_s).
            # Releasing it before iteration starts means the cap protects
            # the OpenAI-call-initiation rate, which is what OpenAI rate
            # limits actually measure against.
            async with _get_semaphore():
                # AsyncOpenAI returns an async-iterable stream when stream=True
                stream = await self.async_client.chat.completions.create(
                    model=self.model,
                    messages=openai_messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    stream=True,
                )

            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

            logger.debug("Streaming AI response completed successfully")

        except Exception as e:
            logger.error(f"OpenAI API streaming error: {str(e)}")
            raise InternalServerError(f"Chat service unavailable: {str(e)}")

    async def send_async(self, messages: List[ChatMessage]) -> str:
        """
        Async chat completion using the native AsyncOpenAI client.

        Awaits the OpenAI call directly — no run_in_executor, no thread pool
        occupancy. Bounded by the module-level concurrency semaphore so a
        request burst can't fan out unbounded against OpenAI's rate limit.

        Args:
            messages: List of conversation messages including system prompt
                and history

        Returns:
            str: AI-generated response content

        Raises:
            InternalServerError: If OpenAI API call fails
        """
        try:
            openai_messages: List[ChatCompletionMessageParam] = [
                cast(ChatCompletionMessageParam, msg.to_dict()) for msg in messages
            ]

            logger.debug(
                f"Generating async AI response from {len(openai_messages)} "
                f"conversations using {self.model}"
            )

            async with _get_semaphore():
                response = await self.async_client.chat.completions.create(
                    model=self.model,
                    messages=openai_messages,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    stream=False,
                )

            reply = response.choices[0].message.content or ""

            logger.debug(
                f"Async AI response generated successfully: {len(reply)} characters"
            )

            return reply

        except Exception as e:
            logger.error(f"OpenAI API async error: {str(e)}")
            raise InternalServerError(f"Chat service unavailable: {str(e)}")


@lru_cache(maxsize=1)
def get_openai_service() -> "OpenAIService":
    """Return a process-wide OpenAIService singleton.

    OpenAIService.__init__ builds both a sync OpenAI and an AsyncOpenAI httpx
    client. Constructing it per request (as several MirrorGPT call sites did)
    rebuilds those clients every time. Caching it — like get_dynamodb_service /
    get_echo_service — reuses the clients across warm-container invocations.
    """
    return OpenAIService()
