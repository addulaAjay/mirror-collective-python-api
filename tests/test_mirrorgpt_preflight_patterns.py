"""Phase 1 — Memory Preflight: Echo Map patterns + recent summary in chat.

Covers the preflight builder (`_load_memory_preflight`), its fetch/render
helpers, and the injection into `process_mirror_chat` (prepended to history on
every turn, gated by MIRRORGPT_PREFLIGHT_PATTERNS).
See docs/MIRRORGPT_MEMORY_PLAN.md Phase 1.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.models.conversation import Conversation
from src.app.models.echo_loop_state import EchoLoopState
from src.app.services.mirror_orchestrator import (
    _PREFLIGHT_MAX_CHARS,
    MirrorOrchestrator,
)
from src.app.services.openai_service import ChatMessage

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _make_orchestrator(*, preflight: bool = True) -> MirrorOrchestrator:
    orch = MirrorOrchestrator(dynamodb_service=AsyncMock(), openai_service=MagicMock())
    # Flag is read from env in __init__; set explicitly for deterministic tests.
    orch.enable_preflight_patterns = preflight
    return orch


def _loop(
    loop_id: str,
    *,
    tone: str = "rising",
    score: float = 0.7,
    label: str = "High",
) -> EchoLoopState:
    return EchoLoopState(
        user_id="user-1",
        loop_id=loop_id,
        tone_state=tone,
        intensity_score=score,
        intensity_label=label,
    )


def _conv(
    conv_id: str,
    *,
    summary: Optional[str] = None,
    threads: Optional[list] = None,
) -> Conversation:
    return Conversation(
        conversation_id=conv_id,
        user_id="user-1",
        summary=summary,
        open_threads=threads,
        last_message_at="2026-07-01T00:00:00Z",
    )


# --------------------------------------------------------------------------
# _load_memory_preflight — flag gating
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_none_and_no_db_when_flag_off():
    """Flag off → returns None instantly without touching the loop store."""
    orch = _make_orchestrator(preflight=False)

    with patch(
        "src.app.repositories.echo_loop_state_repo.EchoLoopStateRepo"
    ) as repo_cls:
        result = await orch._load_preflight_data("user-1", "c-1")

    assert result is None
    repo_cls.assert_not_called()


# --------------------------------------------------------------------------
# _load_memory_preflight — happy path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_packet_includes_patterns_and_summary():
    orch = _make_orchestrator(preflight=True)
    loops = [_loop("grief", tone="rising", score=0.8, label="High")]
    prior = _conv(
        "c-old",
        summary="Processing a recent loss.",
        threads=["hasn't told family"],
    )

    with (
        patch(
            "src.app.repositories.echo_loop_state_repo.EchoLoopStateRepo"
        ) as repo_cls,
        patch("src.app.services.conversation_service.ConversationService") as cs_cls,
    ):
        repo = AsyncMock()
        repo.query_by_user.return_value = loops
        repo_cls.return_value = repo
        cs = AsyncMock()
        cs.get_recent_conversations.return_value = [prior]
        cs_cls.return_value = cs

        data = await orch._load_preflight_data("user-1", "c-current")

    assert data is not None
    loops_out, prior_out = data
    result = orch._render_preflight_packet(loops_out, prior_out)

    assert isinstance(result, ChatMessage)
    assert result.role == "system"
    assert "background only" in result.content.lower()
    assert "grief" in result.content
    # Tone-library guidance line is appended (assert against the real library
    # so this doesn't hard-code copy that may be edited).
    from src.app.services.echo.tone_library_loader import load_tone_library

    expected_line = load_tone_library().lookup("grief", "rising").reflection_line
    assert expected_line in result.content
    # Tier 2 recent summary + open thread.
    assert "Processing a recent loss." in result.content
    assert "hasn't told family" in result.content


@pytest.mark.asyncio
async def test_preflight_degrades_to_summary_when_loops_fail():
    """A loop-store failure must not break the packet — the recent summary
    still comes through (degrade, don't crash)."""
    orch = _make_orchestrator(preflight=True)
    prior = _conv("c-old", summary="Recent reflection about change.")

    with (
        patch(
            "src.app.repositories.echo_loop_state_repo.EchoLoopStateRepo"
        ) as repo_cls,
        patch("src.app.services.conversation_service.ConversationService") as cs_cls,
    ):
        repo = AsyncMock()
        repo.query_by_user.side_effect = RuntimeError("ddb down")
        repo_cls.return_value = repo
        cs = AsyncMock()
        cs.get_recent_conversations.return_value = [prior]
        cs_cls.return_value = cs

        data = await orch._load_preflight_data("user-1", "c-current")

    assert data is not None
    loops_out, prior_out = data
    assert loops_out == []  # loop fetch failed → empty, not raised
    result = orch._render_preflight_packet(loops_out, prior_out)

    assert result is not None
    assert "Recent reflection about change." in result.content
    assert "Active emotional patterns" not in result.content


@pytest.mark.asyncio
async def test_preflight_none_when_no_patterns_and_no_summary():
    orch = _make_orchestrator(preflight=True)

    with (
        patch(
            "src.app.repositories.echo_loop_state_repo.EchoLoopStateRepo"
        ) as repo_cls,
        patch("src.app.services.conversation_service.ConversationService") as cs_cls,
    ):
        repo = AsyncMock()
        repo.query_by_user.return_value = []
        repo_cls.return_value = repo
        cs = AsyncMock()
        cs.get_recent_conversations.return_value = []
        cs_cls.return_value = cs

        data = await orch._load_preflight_data("user-1", "c-current")

    assert data == ([], None)
    assert orch._render_preflight_packet(*data) is None


# --------------------------------------------------------------------------
# _fetch_active_loops — filter + rank + cap
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_active_loops_filters_resolved_and_ranks_top_n():
    orch = _make_orchestrator(preflight=True)
    rows = [
        _loop("pressure", score=0.2, label="Low"),
        _loop("grief", score=0.9, label="High"),
        _loop("agency", score=0.0, label="Low"),  # resolved → filtered out
        _loop("overwhelm", score=0.5, label="Medium"),
        _loop("transition", score=0.7, label="High"),
    ]

    with patch(
        "src.app.repositories.echo_loop_state_repo.EchoLoopStateRepo"
    ) as repo_cls:
        repo = AsyncMock()
        repo.query_by_user.return_value = rows
        repo_cls.return_value = repo

        active = await orch._fetch_active_loops("user-1")

    ids = [lp.loop_id for lp in active]
    # intensity 0 dropped; sorted desc; capped to default top-3.
    assert ids == ["grief", "transition", "overwhelm"]
    assert "agency" not in ids


# --------------------------------------------------------------------------
# _render_preflight_packet — pure rendering
# --------------------------------------------------------------------------


def test_render_none_when_nothing_to_say():
    orch = _make_orchestrator(preflight=True)
    assert orch._render_preflight_packet([], None) is None


def test_render_hard_caps_length():
    orch = _make_orchestrator(preflight=True)
    prior = _conv("c-old", summary="x" * 5000)

    packet = orch._render_preflight_packet([], prior)

    assert packet is not None
    assert len(packet.content) <= _PREFLIGHT_MAX_CHARS


# --------------------------------------------------------------------------
# process_mirror_chat — injection on every turn
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_mirror_chat_prepends_packet_even_with_history():
    """Unlike the carrier, the preflight packet is injected on EVERY turn —
    including when the conversation already has turns — and lands first."""
    from src.app.models.conversation import ConversationMessage

    orch = _make_orchestrator(preflight=True)
    orch.dynamodb_service.get_user_archetype_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    orch.dynamodb_service.save_user_archetype_profile = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )

    # Non-empty history → carrier does NOT fire → Tier 2 is not suppressed.
    orch._load_preflight_data = AsyncMock(  # type: ignore[method-assign]
        return_value=([_loop("grief", tone="rising")], None)
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

    orch.response_generator.generate_enhanced_response = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_generate_enhanced
    )

    with patch(
        "src.app.services.conversation_service.ConversationService"
    ) as mock_cs_class:
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        mock_cs.get_conversation_history.return_value = existing_turns
        mock_cs.get_user_mirrorgpt_signals.return_value = []

        result = await orch.process_mirror_chat(
            user_id="user-1",
            message="continuing the thread",
            session_id="sess-1",
            conversation_id="c-current",
            use_enhanced_response=True,
        )

    assert result["success"] is True
    history = captured["history"]
    # Packet is first, then the real turns.
    assert history[0].role == "system"
    assert "grief" in history[0].content
    assert len(history) == 3
    assert history[1].content == "earlier user msg"


@pytest.mark.asyncio
async def test_process_mirror_chat_suppresses_tier2_summary_when_carrier_fires():
    """On an empty-history turn the carrier injects the recent summary; the
    preflight packet must then carry patterns ONLY (no duplicate summary)."""
    orch = _make_orchestrator(preflight=True)
    orch.dynamodb_service.get_user_archetype_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    orch.dynamodb_service.save_user_archetype_profile = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )

    prior = _conv(
        "c-prior",
        summary="Working through a job change.",
        threads=["hasn't told the manager"],
    )

    captured: dict = {}

    async def fake_generate_enhanced(
        *, user_message, analysis_result, change_analysis, user_context, history
    ):
        captured["history"] = history
        return "generated reply"

    orch.response_generator.generate_enhanced_response = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_generate_enhanced
    )

    with (
        patch(
            "src.app.services.conversation_service.ConversationService"
        ) as mock_cs_class,
        patch(
            "src.app.repositories.echo_loop_state_repo.EchoLoopStateRepo"
        ) as repo_cls,
    ):
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        mock_cs.get_conversation_history.return_value = []  # empty → carrier fires
        mock_cs.get_user_mirrorgpt_signals.return_value = []
        mock_cs.get_recent_conversations.return_value = [prior]
        repo = AsyncMock()
        repo.query_by_user.return_value = [_loop("grief", tone="rising")]
        repo_cls.return_value = repo

        result = await orch.process_mirror_chat(
            user_id="user-1",
            message="thinking about that job thing again",
            session_id="sess-1",
            conversation_id="c-new",
            use_enhanced_response=True,
        )

    assert result["success"] is True
    history = captured["history"]
    # [preflight_packet(patterns only), carrier(summary)]
    packet, carrier = history[0], history[1]
    assert "grief" in packet.content
    assert "Recent reflection" not in packet.content  # Tier 2 suppressed
    assert "Working through a job change." in carrier.content
    # The summary text appears exactly once across the whole context.
    combined = packet.content + carrier.content
    assert combined.count("Working through a job change.") == 1


@pytest.mark.asyncio
async def test_process_mirror_chat_no_packet_when_flag_off():
    """Flag off → no packet in history (empty new conversation stays empty)."""
    orch = _make_orchestrator(preflight=False)
    orch.dynamodb_service.get_user_archetype_profile = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    orch.dynamodb_service.save_user_archetype_profile = AsyncMock(  # type: ignore[method-assign]
        return_value={}
    )

    captured: dict = {}

    async def fake_generate_enhanced(
        *, user_message, analysis_result, change_analysis, user_context, history
    ):
        captured["history"] = history
        return "generated reply"

    orch.response_generator.generate_enhanced_response = AsyncMock(  # type: ignore[method-assign]
        side_effect=fake_generate_enhanced
    )

    with patch(
        "src.app.services.conversation_service.ConversationService"
    ) as mock_cs_class:
        mock_cs = AsyncMock()
        mock_cs_class.return_value = mock_cs
        mock_cs.get_conversation_history.return_value = []
        mock_cs.get_user_mirrorgpt_signals.return_value = []
        mock_cs.get_recent_conversations.return_value = []  # no carrier either

        result = await orch.process_mirror_chat(
            user_id="user-1",
            message="hello",
            session_id="sess-1",
            conversation_id="c-new",
            use_enhanced_response=True,
        )

    assert result["success"] is True
    assert captured["history"] == []
