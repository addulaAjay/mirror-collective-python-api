"""Unit tests for Phase B — greeting continuity in mirrorgpt_routes.

Covers:
- _format_age_label time bucketing
- _load_continuity_context with no prior conversations
- _load_continuity_context with summarized conversations (happy path)
- Lazy-on-read summarization for the most-recent unsummarized conversation
- generate_personalized_greeting with continuity (trigger contains context)
- generate_personalized_greeting cold start (no continuity in trigger)
- generate_personalized_greeting LLM failure fallback

These tests exercise the helpers directly with mocked services so the
fastapi/test client / conftest fixtures aren't needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.api import mirrorgpt_routes
from src.app.models.conversation import Conversation
from src.app.services.openai_service import ChatMessage

# --------------------------------------------------------------------------
# _format_age_label
# --------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def test_format_age_label_returns_recently_when_none():
    assert mirrorgpt_routes._format_age_label(None) == "recently"


def test_format_age_label_handles_unparseable():
    assert mirrorgpt_routes._format_age_label("not-a-date") == "recently"


@pytest.mark.parametrize(
    "minutes_ago,expected",
    [
        (1, "just now"),
        (45, "earlier today"),
        (60 * 8, "today"),
        (60 * 24 + 30, "yesterday"),
        (60 * 24 * 3, "3 days ago"),
        (60 * 24 * 7, "last week"),
        (60 * 24 * 14, "2 weeks ago"),
        (60 * 24 * 60, "a while back"),
    ],
)
def test_format_age_label_buckets(minutes_ago: int, expected: str):
    reference = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    ts = reference - timedelta(minutes=minutes_ago)
    assert mirrorgpt_routes._format_age_label(_iso(ts), now=reference) == expected


# --------------------------------------------------------------------------
# _load_continuity_context
# --------------------------------------------------------------------------


def _conv(
    *,
    conv_id: str,
    summary: Optional[str] = None,
    threads: Optional[List[str]] = None,
    message_count: int = 4,
    last_message_at: Optional[str] = None,
) -> Conversation:
    return Conversation(
        conversation_id=conv_id,
        user_id="user-1",
        message_count=message_count,
        summary=summary,
        open_threads=threads,
        last_message_at=last_message_at
        or _iso(
            datetime.now(timezone.utc) - timedelta(days=2),
        ),
    )


@pytest.mark.asyncio
async def test_load_continuity_no_recent_returns_empty():
    conv_service = MagicMock()
    conv_service.get_recent_conversations = AsyncMock(return_value=[])

    result = await mirrorgpt_routes._load_continuity_context(
        user_id="user-1", conversation_service=conv_service
    )

    assert result["resume_conversation_id"] is None
    assert result["context_lines"] == []
    assert result["has_prior_context"] is False


@pytest.mark.asyncio
async def test_load_continuity_with_summaries_builds_context_lines():
    convs = [
        _conv(
            conv_id="c1",
            summary="Working through avoidance about a job change.",
            threads=["hasn't told the manager yet"],
            last_message_at=_iso(datetime.now(timezone.utc) - timedelta(days=2)),
        ),
        _conv(
            conv_id="c2",
            summary="Reflected on people-pleasing in a friendship.",
            last_message_at=_iso(datetime.now(timezone.utc) - timedelta(days=8)),
        ),
    ]
    conv_service = MagicMock()
    conv_service.get_recent_conversations = AsyncMock(return_value=convs)

    with patch.object(
        mirrorgpt_routes,
        "_try_lazy_summarize",
        new=AsyncMock(return_value=None),
    ):
        result = await mirrorgpt_routes._load_continuity_context(
            user_id="user-1", conversation_service=conv_service
        )

    assert result["resume_conversation_id"] == "c1"
    assert result["has_prior_context"] is True
    assert len(result["context_lines"]) == 2
    assert "Working through avoidance" in result["context_lines"][0]
    assert "Open thread: hasn't told the manager yet." in result["context_lines"][0]
    assert "people-pleasing" in result["context_lines"][1]
    # The first line is tagged so the greeting LLM anchors on the latest
    # conversation instead of picking an older one at random.
    assert result["context_lines"][0].startswith("- (most recent —")
    assert "most recent" not in result["context_lines"][1]


@pytest.mark.asyncio
async def test_load_continuity_refreshes_stale_summary_on_most_recent():
    """Lazy-on-read must run even when the most-recent conversation already
    HAS a summary — staleness is decided inside summarize_if_stale. The old
    behavior (only summarize when missing) served outdated summaries that
    described conversations from several sessions back."""
    stale = _conv(
        conv_id="c1",
        summary="Old summary covering only the first few messages.",
        message_count=20,
        last_message_at=_iso(datetime.now(timezone.utc) - timedelta(hours=1)),
    )
    conv_service = MagicMock()
    conv_service.get_recent_conversations = AsyncMock(return_value=[stale])

    with patch.object(
        mirrorgpt_routes,
        "_try_lazy_summarize",
        new=AsyncMock(return_value=None),
    ) as mock_lazy:
        await mirrorgpt_routes._load_continuity_context(
            user_id="user-1", conversation_service=conv_service
        )

    mock_lazy.assert_awaited_once()


@pytest.mark.asyncio
async def test_try_lazy_summarize_delegates_staleness_to_summarize_if_stale():
    """_try_lazy_summarize must call summarize_if_stale (refresh stale
    summaries), not summarize() gated on summary-missing only."""
    conversation = _conv(conv_id="c1", summary="existing", message_count=10)
    conv_service = MagicMock()

    summarizer_instance = MagicMock()
    summarizer_instance.summarize_if_stale = AsyncMock(return_value=None)

    with (
        patch(
            "src.app.services.conversation_summarizer.ConversationSummarizer",
            return_value=summarizer_instance,
        ),
        patch(
            "src.app.services.openai_service.OpenAIService",
            return_value=MagicMock(),
        ),
    ):
        await mirrorgpt_routes._try_lazy_summarize(
            conversation_service=conv_service,
            conversation=conversation,
            user_id="user-1",
        )

    summarizer_instance.summarize_if_stale.assert_awaited_once_with(
        conversation_id="c1", user_id="user-1"
    )


@pytest.mark.asyncio
async def test_load_continuity_lazy_summarizes_unsummarized_most_recent():
    """Most-recent conversation has no summary → triggers lazy summarize.

    After the summarizer runs and persists, the second fetch picks up the
    new summary and it appears in context_lines.
    """
    unsummarized = _conv(
        conv_id="c1",
        summary=None,
        message_count=6,
        last_message_at=_iso(datetime.now(timezone.utc) - timedelta(hours=3)),
    )
    summarized_after = _conv(
        conv_id="c1",
        summary="Lazy-generated summary about a recent reflection.",
        message_count=6,
        last_message_at=unsummarized.last_message_at,
    )
    conv_service = MagicMock()
    # First call returns unsummarized, second call (after lazy summarize)
    # returns the populated version.
    conv_service.get_recent_conversations = AsyncMock(
        side_effect=[[unsummarized], [summarized_after]]
    )

    with patch.object(
        mirrorgpt_routes,
        "_try_lazy_summarize",
        new=AsyncMock(return_value=None),
    ) as mock_lazy:
        result = await mirrorgpt_routes._load_continuity_context(
            user_id="user-1", conversation_service=conv_service
        )

    mock_lazy.assert_awaited_once()
    assert result["resume_conversation_id"] == "c1"
    assert result["has_prior_context"] is True
    assert "Lazy-generated summary" in result["context_lines"][0]


@pytest.mark.asyncio
async def test_load_continuity_skips_summaries_that_remain_empty():
    """Most-recent has no summary AND lazy summarize fails to produce one.

    The resume id is still surfaced (so the client can reattach), but
    context_lines stays empty → has_prior_context is False.
    """
    unsummarized = _conv(conv_id="c1", summary=None, message_count=2)
    conv_service = MagicMock()
    conv_service.get_recent_conversations = AsyncMock(return_value=[unsummarized])

    with patch.object(
        mirrorgpt_routes,
        "_try_lazy_summarize",
        new=AsyncMock(return_value=None),
    ):
        result = await mirrorgpt_routes._load_continuity_context(
            user_id="user-1", conversation_service=conv_service
        )

    assert result["resume_conversation_id"] == "c1"
    assert result["context_lines"] == []
    assert result["has_prior_context"] is False


@pytest.mark.asyncio
async def test_load_continuity_swallows_loader_errors():
    conv_service = MagicMock()
    conv_service.get_recent_conversations = AsyncMock(
        side_effect=RuntimeError("dynamodb down")
    )

    result = await mirrorgpt_routes._load_continuity_context(
        user_id="user-1", conversation_service=conv_service
    )

    assert result == {
        "resume_conversation_id": None,
        "context_lines": [],
        "has_prior_context": False,
    }


# --------------------------------------------------------------------------
# generate_personalized_greeting
# --------------------------------------------------------------------------


def _make_orchestrator(openai_response: str = "Welcome back, Ajay."):
    orch = MagicMock()
    orch.openai_service = MagicMock()
    orch.openai_service.send_async = AsyncMock(return_value=openai_response)
    return orch


@pytest.mark.asyncio
async def test_greeting_cold_start_uses_simple_trigger_and_names_member():
    orch = _make_orchestrator(openai_response="Hey Ajay, good to have you here.")

    result = await mirrorgpt_routes.generate_personalized_greeting(
        user_context={"id": "user-1", "name": "Ajay"},
        profile=None,
        recent_signals=[],
        recent_moments=[],
        orchestrator=orch,
        continuity=None,
    )

    assert result == "Hey Ajay, good to have you here."
    orch.openai_service.send_async.assert_awaited_once()
    sent_messages: List[ChatMessage] = orch.openai_service.send_async.await_args.args[0]
    trigger = sent_messages[-1].content
    # Cold-start trigger MUST NOT include continuity context block.
    assert "Continuity context" not in trigger
    assert "new user" in trigger or "returning user" in trigger
    # First-name addressing is mandatory when name is available.
    assert "Ajay" in trigger
    assert "MUST address them by their first name" in trigger


@pytest.mark.asyncio
async def test_greeting_with_continuity_mandates_name_and_acknowledgement():
    orch = _make_orchestrator(
        openai_response="Welcome back, Ajay — you mentioned feeling stuck on the job thing."
    )
    continuity = {
        "resume_conversation_id": "c1",
        "context_lines": [
            "- (2 days ago) Working through avoidance about a job change. "
            "Open thread: hasn't told the manager yet."
        ],
        "has_prior_context": True,
    }

    result = await mirrorgpt_routes.generate_personalized_greeting(
        user_context={"id": "user-1", "name": "Ajay"},
        profile=None,
        recent_signals=[],
        recent_moments=[],
        orchestrator=orch,
        continuity=continuity,
    )

    assert "stuck" in result.lower()
    assert "Ajay" in result
    sent_messages: List[ChatMessage] = orch.openai_service.send_async.await_args.args[0]
    trigger = sent_messages[-1].content
    # Continuity-aware trigger MUST carry context block, the mandatory
    # acknowledgement, and the explicit name instruction.
    assert "Continuity context" in trigger
    assert "avoidance about a job change" in trigger
    assert "stance" in trigger.lower()
    assert "do not quote" in trigger.lower()
    assert "MUST acknowledge the prior context" in trigger
    assert "MUST address them by their first name" in trigger
    assert "Ajay" in trigger
    # The trigger must anchor the model on the most recent conversation so
    # the greeting doesn't recap an older conversation at random.
    assert "ordered most recent first" in trigger
    assert "FIRST line" in trigger


@pytest.mark.asyncio
async def test_greeting_omits_name_instruction_when_name_unknown():
    """Master prompt rule: never guess a name. When name is missing, the
    explicit addressing instruction must be omitted and the trigger must
    not contain a stray 'for ' header fragment."""
    orch = _make_orchestrator(openai_response="Hey, what's on your mind?")

    await mirrorgpt_routes.generate_personalized_greeting(
        user_context={"id": "user-1", "name": ""},
        profile=None,
        recent_signals=[],
        recent_moments=[],
        orchestrator=orch,
        continuity=None,
    )

    trigger = orch.openai_service.send_async.await_args.args[0][-1].content
    assert "MUST address them by their first name" not in trigger
    # No dangling " for ." fragment.
    assert "Open a new session." in trigger or "Open a new session for " not in trigger


@pytest.mark.asyncio
async def test_greeting_llm_failure_falls_back_gracefully():
    orch = MagicMock()
    orch.openai_service = MagicMock()
    orch.openai_service.send_async = AsyncMock(side_effect=RuntimeError("api down"))

    result = await mirrorgpt_routes.generate_personalized_greeting(
        user_context={"id": "user-1", "name": "Ajay"},
        profile={"current_archetype_stack": {"primary": "Seeker"}},
        recent_signals=[],
        recent_moments=[],
        orchestrator=orch,
        continuity=None,
    )

    # Returning-user fallback path.
    assert "Ajay" in result
    assert "back" in result.lower() or "mind" in result.lower()


@pytest.mark.asyncio
async def test_greeting_treats_continuity_only_as_returning():
    """Even without profile/signals, prior continuity should mark the user
    as returning so the trigger doesn't say 'new user'."""
    orch = _make_orchestrator()
    continuity = {
        "resume_conversation_id": "c1",
        "context_lines": ["- (yesterday) Brief check-in about anxiety."],
        "has_prior_context": True,
    }

    await mirrorgpt_routes.generate_personalized_greeting(
        user_context={"id": "user-1", "name": ""},
        profile=None,
        recent_signals=[],
        recent_moments=[],
        orchestrator=orch,
        continuity=continuity,
    )

    trigger = orch.openai_service.send_async.await_args.args[0][-1].content
    assert "returning user" in trigger
    assert "new user" not in trigger
