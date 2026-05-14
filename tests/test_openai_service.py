"""
Tests for OpenAIService — focused on the AsyncOpenAI migration and the
module-level concurrency semaphore introduced in Wave 1B-E1.

These tests intentionally do NOT cover the sync paths (`send`,
`send_with_overrides`) — those are unchanged and exercised elsewhere via
integration. The new behaviour under test:
  - send_async / send_with_overrides_async use the AsyncOpenAI client
    natively (no run_in_executor).
  - send_stream consumes an async iterator (no event-loop blocking).
  - The module-level semaphore caps in-flight async calls.
  - Errors from the async client surface as InternalServerError.
"""

import asyncio
import os
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.core.exceptions import InternalServerError
from src.app.services import openai_service as openai_service_module
from src.app.services.openai_service import ChatMessage, OpenAIService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_module_semaphore() -> None:
    """Reset the module-level semaphore between tests.

    Tests that change OPENAI_MAX_INFLIGHT need the cached semaphore cleared,
    otherwise the lazy-init short-circuit will keep returning the original.
    """
    openai_service_module._openai_semaphore = None


def _make_chunk(content: Optional[str]) -> MagicMock:
    """Build a fake streaming chunk shaped like an OpenAI delta."""
    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta = MagicMock()
    chunk.choices[0].delta.content = content
    return chunk


def _make_completion_response(content: str) -> MagicMock:
    """Build a fake non-streaming chat completion response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message = MagicMock()
    response.choices[0].message.content = content
    return response


def _make_messages() -> List[ChatMessage]:
    return [
        ChatMessage("system", "You are a helpful mirror."),
        ChatMessage("user", "Tell me something."),
    ]


@pytest.fixture(autouse=True)
def _ensure_api_key():
    """Make sure the constructor's env-var guard is satisfied."""
    prior = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = "test-openai-key"
    yield
    if prior is None:
        os.environ.pop("OPENAI_API_KEY", None)
    else:
        os.environ["OPENAI_API_KEY"] = prior
    _reset_module_semaphore()


# ---------------------------------------------------------------------------
# send_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_async_uses_async_client():
    """send_async must await the AsyncOpenAI client; sync client is untouched."""
    _reset_module_semaphore()

    fake_async_create = AsyncMock(return_value=_make_completion_response("async reply"))
    fake_sync_create = MagicMock()

    with (
        patch.object(openai_service_module, "AsyncOpenAI") as MockAsync,
        patch.object(openai_service_module, "OpenAI") as MockSync,
    ):
        MockAsync.return_value.chat.completions.create = fake_async_create
        MockSync.return_value.chat.completions.create = fake_sync_create

        service = OpenAIService()
        result = await service.send_async(_make_messages())

    assert result == "async reply"
    fake_async_create.assert_awaited_once()
    fake_sync_create.assert_not_called()


@pytest.mark.asyncio
async def test_send_async_propagates_errors():
    """An exception from the async client becomes InternalServerError."""
    _reset_module_semaphore()

    fake_async_create = AsyncMock(side_effect=RuntimeError("boom"))

    with (
        patch.object(openai_service_module, "AsyncOpenAI") as MockAsync,
        patch.object(openai_service_module, "OpenAI"),
    ):
        MockAsync.return_value.chat.completions.create = fake_async_create

        service = OpenAIService()
        with pytest.raises(InternalServerError) as exc_info:
            await service.send_async(_make_messages())

    assert "boom" in str(exc_info.value) or "Chat service unavailable" in str(
        exc_info.value
    )


# ---------------------------------------------------------------------------
# send_with_overrides_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_with_overrides_async_uses_async_client():
    """Overrides path also awaits the async client and forwards params."""
    _reset_module_semaphore()

    fake_async_create = AsyncMock(
        return_value=_make_completion_response("override reply")
    )
    fake_sync_create = MagicMock()

    with (
        patch.object(openai_service_module, "AsyncOpenAI") as MockAsync,
        patch.object(openai_service_module, "OpenAI") as MockSync,
    ):
        MockAsync.return_value.chat.completions.create = fake_async_create
        MockSync.return_value.chat.completions.create = fake_sync_create

        service = OpenAIService()
        result = await service.send_with_overrides_async(
            _make_messages(),
            model="gpt-4o",
            temperature=0.2,
            max_tokens=120,
        )

    assert result == "override reply"
    fake_async_create.assert_awaited_once()
    fake_sync_create.assert_not_called()

    # Verify overrides actually reached the call.
    _, call_kwargs = fake_async_create.call_args
    assert call_kwargs["model"] == "gpt-4o"
    assert call_kwargs["temperature"] == 0.2
    assert call_kwargs["max_tokens"] == 120
    assert call_kwargs["stream"] is False


# ---------------------------------------------------------------------------
# send_stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_stream_uses_async_iterator():
    """send_stream must consume an async iterator and yield chunk content."""
    _reset_module_semaphore()

    chunks = [_make_chunk("hello "), _make_chunk("world"), _make_chunk(None)]

    class _FakeStream:
        def __init__(self, items):
            self._items = list(items)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

    fake_create = AsyncMock(return_value=_FakeStream(chunks))

    with (
        patch.object(openai_service_module, "AsyncOpenAI") as MockAsync,
        patch.object(openai_service_module, "OpenAI"),
    ):
        MockAsync.return_value.chat.completions.create = fake_create

        service = OpenAIService()
        out = []
        async for piece in service.send_stream(_make_messages()):
            out.append(piece)

    assert out == ["hello ", "world"]
    fake_create.assert_awaited_once()
    _, call_kwargs = fake_create.call_args
    assert call_kwargs["stream"] is True


# ---------------------------------------------------------------------------
# Concurrency semaphore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semaphore_caps_concurrency(monkeypatch):
    """At most _OPENAI_MAX_INFLIGHT calls run concurrently."""
    # Reload-style: change the cap and reset the cached semaphore.
    monkeypatch.setattr(openai_service_module, "_OPENAI_MAX_INFLIGHT", 2)
    _reset_module_semaphore()

    inflight = 0
    peak = 0
    lock = asyncio.Lock()

    async def slow_create(*_args, **_kwargs):
        nonlocal inflight, peak
        async with lock:
            inflight += 1
            peak = max(peak, inflight)
        try:
            # Hold the slot long enough that other tasks pile up against
            # the semaphore.
            await asyncio.sleep(0.05)
            return _make_completion_response("ok")
        finally:
            async with lock:
                inflight -= 1

    with (
        patch.object(openai_service_module, "AsyncOpenAI") as MockAsync,
        patch.object(openai_service_module, "OpenAI"),
    ):
        MockAsync.return_value.chat.completions.create = AsyncMock(
            side_effect=slow_create
        )

        service = OpenAIService()

        results = await asyncio.gather(
            *(service.send_async(_make_messages()) for _ in range(5))
        )

    assert all(r == "ok" for r in results)
    assert peak <= 2, f"Concurrency cap breached: peak={peak}"
    # Confirm we actually exercised concurrency (not all serial).
    assert peak >= 2, f"Did not exercise parallelism: peak={peak}"
