"""Phase C tests — prior-conversation continuity carrier in chat.

When a user opens a brand-new conversation, the orchestrator should
inject the prior conversation's summary as a synthetic system message so
the model can pick up the thread across the conversation boundary.
See docs/MIRRORGPT_CONTINUITY_MEMORY.md.

These tests exercise both the unit method (`_load_prior_continuity_carrier`)
and its integration into `process_mirror_chat`.
"""

from __future__ import annotations

from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.models.conversation import Conversation
from src.app.services.mirror_orchestrator import MirrorOrchestrator
from src.app.services.openai_service import ChatMessage

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _conv(
    *,
    conv_id: str,
    summary: Optional[str] = None,
    threads: Optional[List[str]] = None,
    message_count: int = 4,
) -> Conversation:
    return Conversation(
        conversation_id=conv_id,
        user_id="user-1",
        message_count=message_count,
        summary=summary,
        open_threads=threads,
        last_message_at="2026-05-08T00:00:00Z",
    )


def _make_orchestrator() -> MirrorOrchestrator:
    dynamodb = AsyncMock()
    openai = MagicMock()
    return MirrorOrchestrator(dynamodb_service=dynamodb, openai_service=openai)


# --------------------------------------------------------------------------
# _load_prior_continuity_carrier — unit
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_carrier_none_when_no_recent_conversations():
    orchestrator = _make_orchestrator()

    with patch(
        "src.app.services.conversation_service.ConversationService"
    ) as mock_cs_class:
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        mock_cs.get_recent_conversations.return_value = []

        result = await orchestrator._load_prior_continuity_carrier(
            user_id="user-1", current_conversation_id="c-new"
        )

    assert result is None


@pytest.mark.asyncio
async def test_carrier_none_when_only_current_conversation_present():
    """The only conversation is the current one → no prior context."""
    orchestrator = _make_orchestrator()

    with patch(
        "src.app.services.conversation_service.ConversationService"
    ) as mock_cs_class:
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        mock_cs.get_recent_conversations.return_value = [
            _conv(conv_id="c-new", summary="something")
        ]

        result = await orchestrator._load_prior_continuity_carrier(
            user_id="user-1", current_conversation_id="c-new"
        )

    assert result is None


@pytest.mark.asyncio
async def test_carrier_built_when_prior_summarized():
    orchestrator = _make_orchestrator()
    prior = _conv(
        conv_id="c-prior",
        summary="Working through avoidance about a job change.",
        threads=["hasn't told the manager yet"],
    )

    with patch(
        "src.app.services.conversation_service.ConversationService"
    ) as mock_cs_class:
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        mock_cs.get_recent_conversations.return_value = [prior]

        result = await orchestrator._load_prior_continuity_carrier(
            user_id="user-1", current_conversation_id="c-new"
        )

    assert result is not None
    assert isinstance(result, ChatMessage)
    assert result.role == "system"
    # Carrier text must mark itself as background, include the summary,
    # and reference the open thread.
    assert "background only" in result.content.lower()
    assert "do not quote" in result.content.lower()
    assert "Working through avoidance" in result.content
    assert "hasn't told the manager yet" in result.content


@pytest.mark.asyncio
async def test_carrier_none_when_prior_unsummarized_and_too_few_messages():
    """Prior is the candidate but has fewer than the summary threshold's
    messages and no summary → can't summarize on the fly, and there is no
    other summarized conversation to fall through to → no carrier."""
    orchestrator = _make_orchestrator()
    prior = _conv(conv_id="c-prior", summary=None, message_count=1)

    with patch(
        "src.app.services.conversation_service.ConversationService"
    ) as mock_cs_class:
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        mock_cs.get_recent_conversations.return_value = [prior]

        result = await orchestrator._load_prior_continuity_carrier(
            user_id="user-1", current_conversation_id="c-new"
        )

    assert result is None


@pytest.mark.asyncio
async def test_carrier_lazy_summarizes_eligible_prior():
    """Prior has enough messages but no summary → lazy summarize → carrier."""
    orchestrator = _make_orchestrator()
    unsummarized = _conv(conv_id="c-prior", summary=None, message_count=6)
    summarized_after = _conv(
        conv_id="c-prior",
        summary="Recent reflection about indecision.",
        message_count=6,
    )

    with (
        patch(
            "src.app.services.conversation_service.ConversationService"
        ) as mock_cs_class,
        patch(
            "src.app.services.conversation_summarizer.ConversationSummarizer"
        ) as mock_summarizer_class,
    ):
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        mock_cs.get_recent_conversations.side_effect = [
            [unsummarized],
            [summarized_after],
        ]
        mock_summarizer = MagicMock()
        mock_summarizer.summarize = AsyncMock(return_value=None)
        mock_summarizer_class.return_value = mock_summarizer

        result = await orchestrator._load_prior_continuity_carrier(
            user_id="user-1", current_conversation_id="c-new"
        )

    mock_summarizer.summarize.assert_awaited_once_with(
        conversation_id="c-prior", user_id="user-1"
    )
    assert result is not None
    assert "Recent reflection about indecision" in result.content


@pytest.mark.asyncio
async def test_carrier_returns_none_when_loader_raises():
    """Errors fetching recent conversations must not break chat — None."""
    orchestrator = _make_orchestrator()

    with patch(
        "src.app.services.conversation_service.ConversationService"
    ) as mock_cs_class:
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        mock_cs.get_recent_conversations.side_effect = RuntimeError("dynamo down")

        result = await orchestrator._load_prior_continuity_carrier(
            user_id="user-1", current_conversation_id="c-new"
        )

    assert result is None


@pytest.mark.asyncio
async def test_carrier_picks_most_recent_prior_skipping_current():
    """Carrier should reflect the most-recent OTHER conversation."""
    orchestrator = _make_orchestrator()
    current = _conv(conv_id="c-new", summary="current summary")
    prior_1 = _conv(conv_id="c-prior-1", summary="most recent prior")
    prior_2 = _conv(conv_id="c-prior-2", summary="older prior")

    with patch(
        "src.app.services.conversation_service.ConversationService"
    ) as mock_cs_class:
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        # Recent list is ordered newest-first by GSI.
        mock_cs.get_recent_conversations.return_value = [current, prior_1, prior_2]

        result = await orchestrator._load_prior_continuity_carrier(
            user_id="user-1", current_conversation_id="c-new"
        )

    assert result is not None
    assert "most recent prior" in result.content
    assert "older prior" not in result.content
    assert "current summary" not in result.content


# --------------------------------------------------------------------------
# process_mirror_chat integration — carrier flows into LLM call
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_mirror_chat_injects_carrier_when_history_empty():
    """Empty current history + prior summary present → carrier reaches LLM."""
    orchestrator = _make_orchestrator()
    orchestrator.dynamodb_service.get_user_archetype_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    orchestrator.dynamodb_service.save_user_archetype_profile = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )

    prior = _conv(
        conv_id="c-prior",
        summary="Working through avoidance about a job change.",
        threads=["hasn't told the manager yet"],
    )

    # Capture what history the response generator sees.
    captured: dict = {}

    async def fake_generate_enhanced(
        *, user_message, analysis_result, change_analysis, user_context, history
    ):
        captured["history"] = history
        return "generated reply"

    orchestrator.response_generator.generate_enhanced_response = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_generate_enhanced
    )

    with patch(
        "src.app.services.conversation_service.ConversationService"
    ) as mock_cs_class:
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        # Current-conversation history is empty (new conversation).
        mock_cs.get_conversation_history.return_value = []
        # Mirrorgpt signals (separate code path) — also empty.
        mock_cs.get_user_mirrorgpt_signals.return_value = []
        # Carrier loader sees one prior.
        mock_cs.get_recent_conversations.return_value = [prior]

        result = await orchestrator.process_mirror_chat(
            user_id="user-1",
            message="Hey, I've been thinking again about that job thing.",
            session_id="sess-1",
            conversation_id="c-new",
            use_enhanced_response=True,
        )

    assert result["success"] is True
    history = captured["history"]
    assert isinstance(history, list)
    assert len(history) == 1, "Carrier should be the only history item for a new convo"
    assert history[0].role == "system"
    assert "Working through avoidance" in history[0].content


@pytest.mark.asyncio
async def test_process_mirror_chat_skips_carrier_when_history_present():
    """If the current convo already has turns, the carrier is NOT added —
    raw turns take precedence."""
    from src.app.models.conversation import ConversationMessage

    orchestrator = _make_orchestrator()
    orchestrator.dynamodb_service.get_user_archetype_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    orchestrator.dynamodb_service.save_user_archetype_profile = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )

    existing_turns = [
        ConversationMessage(
            message_id="m1",
            conversation_id="c-current",
            role="user",
            content="earlier user msg",
            timestamp="2026-05-09T00:00:00Z",
        ),
        ConversationMessage(
            message_id="m2",
            conversation_id="c-current",
            role="assistant",
            content="earlier assistant reply",
            timestamp="2026-05-09T00:00:01Z",
        ),
    ]

    captured: dict = {}

    async def fake_generate_enhanced(
        *, user_message, analysis_result, change_analysis, user_context, history
    ):
        captured["history"] = history
        return "generated reply"

    orchestrator.response_generator.generate_enhanced_response = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_generate_enhanced
    )
    # Spy on the carrier method — must NOT be called when history exists.
    orchestrator._load_prior_continuity_carrier = AsyncMock()  # type: ignore[method-assign]

    with patch(
        "src.app.services.conversation_service.ConversationService"
    ) as mock_cs_class:
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        mock_cs.get_conversation_history.return_value = existing_turns
        mock_cs.get_user_mirrorgpt_signals.return_value = []

        result = await orchestrator.process_mirror_chat(
            user_id="user-1",
            message="continuing the thread",
            session_id="sess-1",
            conversation_id="c-current",
            use_enhanced_response=True,
        )

    assert result["success"] is True
    orchestrator._load_prior_continuity_carrier.assert_not_awaited()
    history = captured["history"]
    assert len(history) == 2
    assert all(m.role in ("user", "assistant") for m in history)
    assert not any("background only" in (m.content or "").lower() for m in history)


@pytest.mark.asyncio
async def test_process_mirror_chat_history_empty_and_no_prior_yields_empty_history():
    """No prior conversations at all → history stays empty, no carrier."""
    orchestrator = _make_orchestrator()
    orchestrator.dynamodb_service.get_user_archetype_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    orchestrator.dynamodb_service.save_user_archetype_profile = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )

    captured: dict = {}

    async def fake_generate_enhanced(
        *, user_message, analysis_result, change_analysis, user_context, history
    ):
        captured["history"] = history
        return "generated reply"

    orchestrator.response_generator.generate_enhanced_response = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_generate_enhanced
    )

    with patch(
        "src.app.services.conversation_service.ConversationService"
    ) as mock_cs_class:
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        mock_cs.get_conversation_history.return_value = []
        mock_cs.get_user_mirrorgpt_signals.return_value = []
        mock_cs.get_recent_conversations.return_value = []

        result = await orchestrator.process_mirror_chat(
            user_id="user-1",
            message="first message ever",
            session_id="sess-1",
            conversation_id="c-new",
            use_enhanced_response=True,
        )

    assert result["success"] is True
    assert captured["history"] == []


@pytest.mark.asyncio
async def test_carrier_falls_through_to_older_summarized_conversation():
    """Most-recent prior has no summary and is too short to summarize on the
    fly, but an older conversation already has one → the carrier uses the
    older summary instead of dropping continuity (no extra fetch/model call).
    """
    orchestrator = _make_orchestrator()
    recent_no_summary = _conv(conv_id="c-recent", summary=None, message_count=1)
    older_summarized = _conv(
        conv_id="c-old",
        summary="Earlier work on setting boundaries with family.",
        threads=["hasn't raised it at dinner yet"],
        message_count=8,
    )

    with patch(
        "src.app.services.conversation_service.ConversationService"
    ) as mock_cs_class:
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        # Most-recent first (no summary), older summarized second.
        mock_cs.get_recent_conversations.return_value = [
            recent_no_summary,
            older_summarized,
        ]

        result = await orchestrator._load_prior_continuity_carrier(
            user_id="user-1", current_conversation_id="c-new"
        )

    assert result is not None
    assert result.role == "system"
    assert "setting boundaries with family" in result.content
    assert "hasn't raised it at dinner yet" in result.content
    # Fall-through must not require a second recent-conversations read.
    mock_cs.get_recent_conversations.assert_awaited_once()
