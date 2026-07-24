"""Unit tests for ConversationSummarizer.

Mocks OpenAIService and ConversationService directly — these tests don't
touch DynamoDB or any network resource. See
docs/MIRRORGPT_CONTINUITY_MEMORY.md for the design.
"""

from __future__ import annotations

import json
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.app.models.conversation import Conversation, ConversationMessage, KeyTheme
from src.app.services.conversation_summarizer import (
    SUMMARIZER_SYSTEM_PROMPT,
    ConversationSummarizer,
    SummaryResult,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _make_message(
    msg_id: str, role: str, content: str, conv_id: str = "conv-1"
) -> ConversationMessage:
    return ConversationMessage(
        message_id=msg_id,
        conversation_id=conv_id,
        role=role,  # type: ignore[arg-type]
        content=content,
        timestamp="2026-05-10T00:00:00Z",
    )


def _make_conversation(
    *,
    message_count: int = 0,
    summary: Optional[str] = None,
    summarized_through: Optional[str] = None,
) -> Conversation:
    return Conversation(
        conversation_id="conv-1",
        user_id="user-1",
        message_count=message_count,
        summary=summary,
        key_themes=[KeyTheme("existing-theme")] if summary else None,
        open_threads=["existing-thread"] if summary else None,
        summarized_through_message_id=summarized_through,
        summarized_at="2026-05-09T00:00:00Z" if summary else None,
    )


def _make_summarizer(
    *,
    conversation: Conversation,
    history: List[ConversationMessage],
    openai_response: str,
    first_summary_at: int = 4,
    refresh_threshold: int = 6,
) -> tuple[ConversationSummarizer, MagicMock, MagicMock]:
    openai_service = MagicMock()
    openai_service.send_with_overrides_async = AsyncMock(return_value=openai_response)

    conv_service = MagicMock()
    conv_service.get_conversation = AsyncMock(return_value=conversation)
    conv_service.get_conversation_history = AsyncMock(return_value=history)
    conv_service.update_conversation_summary = AsyncMock(return_value=conversation)

    summarizer = ConversationSummarizer(
        openai_service=openai_service,
        conversation_service=conv_service,
        first_summary_at=first_summary_at,
        refresh_threshold=refresh_threshold,
    )
    return summarizer, openai_service, conv_service


# --------------------------------------------------------------------------
# summarize() — happy path & error handling
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_happy_path_persists_result():
    history = [
        _make_message("m1", "user", "I'm stuck on whether to quit my job."),
        _make_message("m2", "assistant", "What's the part you keep avoiding?"),
        _make_message("m3", "user", "Telling my manager."),
        _make_message("m4", "assistant", "That sounds like avoidance, not indecision."),
    ]
    conv = _make_conversation(message_count=4)
    openai_payload = json.dumps(
        {
            "summary": "Working through avoidance about quitting a job.",
            "key_themes": ["career indecision", "avoidance"],
            "open_threads": ["hasn't told the manager yet"],
        }
    )
    summarizer, openai_service, conv_service = _make_summarizer(
        conversation=conv, history=history, openai_response=openai_payload
    )

    result = await summarizer.summarize("conv-1", "user-1")

    assert result is not None
    assert isinstance(result, SummaryResult)
    assert result.summary.startswith("Working through avoidance")
    # V1 plain-string themes normalize to low-confidence KeyTheme objects.
    assert [t.theme for t in result.key_themes] == [
        "career indecision",
        "avoidance",
    ]
    assert all(t.confidence == "low" for t in result.key_themes)
    assert result.open_threads == ["hasn't told the manager yet"]
    assert result.summarized_through_message_id == "m4"
    assert result.summarized_at  # populated

    openai_service.send_with_overrides_async.assert_awaited_once()
    conv_service.update_conversation_summary.assert_awaited_once()
    # The persisted Conversation should carry the new fields.
    persisted = conv_service.update_conversation_summary.await_args.args[0]
    assert persisted.summary == result.summary
    assert persisted.key_themes == result.key_themes
    assert persisted.open_threads == result.open_threads
    assert persisted.summarized_through_message_id == "m4"


@pytest.mark.asyncio
async def test_summarize_below_first_threshold_returns_none():
    conv = _make_conversation(message_count=2)
    summarizer, openai_service, _ = _make_summarizer(
        conversation=conv,
        history=[],
        openai_response="",
        first_summary_at=4,
    )

    result = await summarizer.summarize("conv-1", "user-1")

    assert result is None
    openai_service.send_with_overrides_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_summarize_handles_openai_error_gracefully():
    history = [
        _make_message("m1", "user", "Hi"),
        _make_message("m2", "assistant", "Hey"),
        _make_message("m3", "user", "I'm stuck"),
        _make_message("m4", "assistant", "What's the pattern?"),
    ]
    conv = _make_conversation(message_count=4)
    summarizer, openai_service, conv_service = _make_summarizer(
        conversation=conv, history=history, openai_response=""
    )
    openai_service.send_with_overrides_async = AsyncMock(
        side_effect=RuntimeError("boom")
    )

    result = await summarizer.summarize("conv-1", "user-1")

    assert result is None
    conv_service.update_conversation_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_summarize_rejects_malformed_json():
    history = [
        _make_message("m1", "user", "test"),
        _make_message("m2", "assistant", "ok"),
        _make_message("m3", "user", "more"),
        _make_message("m4", "assistant", "ok"),
    ]
    conv = _make_conversation(message_count=4)
    summarizer, _, conv_service = _make_summarizer(
        conversation=conv,
        history=history,
        openai_response="this is not json at all",
    )

    result = await summarizer.summarize("conv-1", "user-1")

    assert result is None
    conv_service.update_conversation_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_summarize_strips_code_fences():
    history = [
        _make_message("m1", "user", "test"),
        _make_message("m2", "assistant", "ok"),
        _make_message("m3", "user", "more"),
        _make_message("m4", "assistant", "ok"),
    ]
    conv = _make_conversation(message_count=4)
    fenced = (
        "```json\n"
        + json.dumps(
            {
                "summary": "Brief check-in.",
                "key_themes": ["check-in"],
                "open_threads": [],
            }
        )
        + "\n```"
    )
    summarizer, _, _ = _make_summarizer(
        conversation=conv, history=history, openai_response=fenced
    )

    result = await summarizer.summarize("conv-1", "user-1")

    assert result is not None
    assert result.summary == "Brief check-in."
    assert [t.theme for t in result.key_themes] == ["check-in"]
    assert result.open_threads == []


@pytest.mark.asyncio
async def test_summarize_rejects_wrong_types_in_json():
    history = [
        _make_message("m1", "user", "test"),
        _make_message("m2", "assistant", "ok"),
        _make_message("m3", "user", "more"),
        _make_message("m4", "assistant", "ok"),
    ]
    conv = _make_conversation(message_count=4)
    # themes should be a list of strings; here it's a string.
    bad_payload = json.dumps(
        {"summary": "ok", "key_themes": "not-a-list", "open_threads": []}
    )
    summarizer, _, conv_service = _make_summarizer(
        conversation=conv, history=history, openai_response=bad_payload
    )

    result = await summarizer.summarize("conv-1", "user-1")

    assert result is None
    conv_service.update_conversation_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_summarize_caps_themes_and_threads_lengths():
    history = [
        _make_message("m1", "user", "test"),
        _make_message("m2", "assistant", "ok"),
        _make_message("m3", "user", "more"),
        _make_message("m4", "assistant", "ok"),
    ]
    conv = _make_conversation(message_count=4)
    payload = json.dumps(
        {
            "summary": "lots going on",
            "key_themes": ["t1", "t2", "t3", "t4", "t5", "t6"],
            "open_threads": ["a", "b", "c", "d", "e"],
        }
    )
    summarizer, _, _ = _make_summarizer(
        conversation=conv, history=history, openai_response=payload
    )

    result = await summarizer.summarize("conv-1", "user-1")

    assert result is not None
    assert len(result.key_themes) == 4
    assert len(result.open_threads) == 3


# --------------------------------------------------------------------------
# summarize_if_stale() — threshold/staleness logic
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_if_stale_skips_when_under_first_threshold():
    conv = _make_conversation(message_count=2)
    summarizer, openai_service, _ = _make_summarizer(
        conversation=conv,
        history=[],
        openai_response="",
        first_summary_at=4,
    )

    result = await summarizer.summarize_if_stale("conv-1", "user-1")

    assert result is None
    openai_service.send_with_overrides_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_summarize_if_stale_generates_when_no_summary_yet():
    history = [_make_message(f"m{i}", "user", "x") for i in range(1, 5)]
    conv = _make_conversation(message_count=4, summary=None)
    payload = json.dumps(
        {"summary": "first summary", "key_themes": [], "open_threads": []}
    )
    summarizer, openai_service, _ = _make_summarizer(
        conversation=conv, history=history, openai_response=payload
    )

    result = await summarizer.summarize_if_stale("conv-1", "user-1")

    assert result is not None
    assert result.summary == "first summary"
    openai_service.send_with_overrides_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_summarize_if_stale_returns_existing_when_fresh():
    # Conversation has summary marked through m4, history also ends at m4.
    history = [_make_message(f"m{i}", "user", "x") for i in range(1, 5)]
    conv = _make_conversation(
        message_count=4,
        summary="existing summary",
        summarized_through="m4",
    )
    summarizer, openai_service, conv_service = _make_summarizer(
        conversation=conv,
        history=history,
        openai_response="",
        refresh_threshold=6,
    )

    result = await summarizer.summarize_if_stale("conv-1", "user-1")

    assert result is not None
    assert result.summary == "existing summary"
    # No regeneration.
    openai_service.send_with_overrides_async.assert_not_awaited()
    conv_service.update_conversation_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_summarize_if_stale_regenerates_when_threshold_exceeded():
    # Summary marker is m4, but history has 10 messages — 6 new since marker.
    history = [_make_message(f"m{i}", "user", "x") for i in range(1, 11)]
    conv = _make_conversation(
        message_count=10,
        summary="stale summary",
        summarized_through="m4",
    )
    payload = json.dumps(
        {"summary": "fresh summary", "key_themes": [], "open_threads": []}
    )
    summarizer, openai_service, conv_service = _make_summarizer(
        conversation=conv,
        history=history,
        openai_response=payload,
        refresh_threshold=6,
    )

    result = await summarizer.summarize_if_stale("conv-1", "user-1")

    assert result is not None
    assert result.summary == "fresh summary"
    openai_service.send_with_overrides_async.assert_awaited_once()
    conv_service.update_conversation_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_summarize_if_stale_regenerates_when_marker_not_in_window():
    # Marker references an old message no longer in our recent window —
    # treat as very stale.
    history = [_make_message(f"m{i}", "user", "x") for i in range(20, 25)]
    conv = _make_conversation(
        message_count=24,
        summary="ancient",
        summarized_through="m-old",  # not in history
    )
    payload = json.dumps({"summary": "refreshed", "key_themes": [], "open_threads": []})
    summarizer, openai_service, _ = _make_summarizer(
        conversation=conv, history=history, openai_response=payload
    )

    result = await summarizer.summarize_if_stale("conv-1", "user-1")

    assert result is not None
    assert result.summary == "refreshed"
    openai_service.send_with_overrides_async.assert_awaited_once()


# --------------------------------------------------------------------------
# V2 schema — confidence-tagged themes + nudge
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summarize_parses_object_themes_with_confidence():
    history = [_make_message(f"m{i}", "user", "x") for i in range(1, 5)]
    conv = _make_conversation(message_count=4)
    payload = json.dumps(
        {
            "summary": "Working through avoidance.",
            "key_themes": [
                {"theme": "avoidance", "confidence": "high"},
                {"theme": "career indecision", "confidence": "medium"},
            ],
            "open_threads": [],
            "nudge": {"eligible": True, "reason": "A hard conversation is pending."},
        }
    )
    summarizer, _, _ = _make_summarizer(
        conversation=conv, history=history, openai_response=payload
    )

    result = await summarizer.summarize("conv-1", "user-1")

    assert result is not None
    assert result.key_themes == [
        KeyTheme("avoidance", "high"),
        KeyTheme("career indecision", "medium"),
    ]
    assert result.nudge_eligible is True
    assert result.nudge_reason == "A hard conversation is pending."


@pytest.mark.asyncio
async def test_summarize_clamps_bad_confidence_and_drops_junk_themes():
    history = [_make_message(f"m{i}", "user", "x") for i in range(1, 5)]
    conv = _make_conversation(message_count=4)
    payload = json.dumps(
        {
            "summary": "ok",
            "key_themes": [
                {"theme": "boundary setting", "confidence": "SUPER"},  # -> low
                {"confidence": "high"},  # no theme -> dropped
                {"theme": "  ", "confidence": "low"},  # empty theme -> dropped
                "people-pleasing",  # legacy string -> low
            ],
            "open_threads": [],
        }
    )
    summarizer, _, _ = _make_summarizer(
        conversation=conv, history=history, openai_response=payload
    )

    result = await summarizer.summarize("conv-1", "user-1")

    assert result is not None
    assert result.key_themes == [
        KeyTheme("boundary setting", "low"),
        KeyTheme("people-pleasing", "low"),
    ]


@pytest.mark.asyncio
async def test_summarize_defaults_nudge_when_absent():
    history = [_make_message(f"m{i}", "user", "x") for i in range(1, 5)]
    conv = _make_conversation(message_count=4)
    payload = json.dumps({"summary": "brief", "key_themes": [], "open_threads": []})
    summarizer, _, _ = _make_summarizer(
        conversation=conv, history=history, openai_response=payload
    )

    result = await summarizer.summarize("conv-1", "user-1")

    assert result is not None
    assert result.nudge_eligible is False
    assert result.nudge_reason == ""


@pytest.mark.asyncio
async def test_summarize_blanks_reason_when_not_eligible():
    history = [_make_message(f"m{i}", "user", "x") for i in range(1, 5)]
    conv = _make_conversation(message_count=4)
    payload = json.dumps(
        {
            "summary": "brief",
            "key_themes": [],
            "open_threads": [],
            "nudge": {"eligible": False, "reason": "leftover reason"},
        }
    )
    summarizer, _, _ = _make_summarizer(
        conversation=conv, history=history, openai_response=payload
    )

    result = await summarizer.summarize("conv-1", "user-1")

    assert result is not None
    assert result.nudge_eligible is False
    assert result.nudge_reason == ""


@pytest.mark.asyncio
async def test_summarize_persists_nudge_and_object_themes():
    history = [_make_message(f"m{i}", "user", "x") for i in range(1, 5)]
    conv = _make_conversation(message_count=4)
    payload = json.dumps(
        {
            "summary": "s",
            "key_themes": [{"theme": "avoidance", "confidence": "high"}],
            "open_threads": [],
            "nudge": {"eligible": True, "reason": "pending decision"},
        }
    )
    summarizer, _, conv_service = _make_summarizer(
        conversation=conv, history=history, openai_response=payload
    )

    await summarizer.summarize("conv-1", "user-1")

    persisted = conv_service.update_conversation_summary.await_args.args[0]
    assert persisted.key_themes == [KeyTheme("avoidance", "high")]
    assert persisted.nudge_eligible is True
    assert persisted.nudge_reason == "pending decision"


# --------------------------------------------------------------------------
# Conversation model — theme normalization + nudge round-trip
# --------------------------------------------------------------------------


def test_from_dynamodb_item_normalizes_legacy_string_themes():
    conv = Conversation.from_dynamodb_item(
        {
            "conversation_id": "c",
            "user_id": "u",
            "key_themes": ["avoidance", "people-pleasing"],
        }
    )
    assert conv.key_themes == [
        KeyTheme("avoidance", "low"),
        KeyTheme("people-pleasing", "low"),
    ]
    assert conv.nudge_eligible is False
    assert conv.nudge_reason == ""


def test_from_dynamodb_item_reads_object_themes_and_nudge():
    conv = Conversation.from_dynamodb_item(
        {
            "conversation_id": "c",
            "user_id": "u",
            "key_themes": [{"theme": "avoidance", "confidence": "high"}],
            "nudge_eligible": True,
            "nudge_reason": "pending decision",
        }
    )
    assert conv.key_themes == [KeyTheme("avoidance", "high")]
    assert conv.nudge_eligible is True
    assert conv.nudge_reason == "pending decision"


# --------------------------------------------------------------------------
# Prompt anchor tests
#
# These guard against silent regressions where a future edit drops a safety
# rule from the summarizer prompt. They assert the *string* anchors are
# present in the constant; they do not call the LLM.
# --------------------------------------------------------------------------


def test_summarizer_prompt_enforces_json_only_output():
    """Output contract: model must return ONLY the JSON object."""
    assert "Return ONLY the JSON object" in SUMMARIZER_SYSTEM_PROMPT
    assert "JSON must be valid" in SUMMARIZER_SYSTEM_PROMPT


def test_summarizer_prompt_forbids_mental_health_diagnosis():
    """Critical safety rule: no diagnosing/labeling mental-health conditions."""
    assert "Do NOT diagnose, label, or speculate" in SUMMARIZER_SYSTEM_PROMPT
    assert "mental health conditions" in SUMMARIZER_SYSTEM_PROMPT
    assert "personality disorders" in SUMMARIZER_SYSTEM_PROMPT
    assert "attachment styles" in SUMMARIZER_SYSTEM_PROMPT


def test_summarizer_prompt_forbids_third_party_identifiers():
    """Privacy: no identifying details about people in the user's life."""
    assert "third-party names" in SUMMARIZER_SYSTEM_PROMPT
    assert "phone numbers" in SUMMARIZER_SYSTEM_PROMPT
    assert "school names" in SUMMARIZER_SYSTEM_PROMPT
    assert "usernames" in SUMMARIZER_SYSTEM_PROMPT


def test_summarizer_prompt_carries_anti_oracle_vocab():
    """Anti-oracle: banned vocabulary list must be present verbatim."""
    for token in [
        "sacred",
        "seeker",
        "soul",
        "spirit",
        "cosmic",
        "resonance",
        "the mirror remembers",
    ]:
        assert token in SUMMARIZER_SYSTEM_PROMPT, f"missing banned token: {token!r}"


def test_summarizer_prompt_forbids_advice_and_coaching():
    """Summaries are continuity memory, not coaching output."""
    assert "Do NOT include advice" in SUMMARIZER_SYSTEM_PROMPT
    assert "coaching" in SUMMARIZER_SYSTEM_PROMPT
    assert "suggested actions" in SUMMARIZER_SYSTEM_PROMPT


def test_summarizer_prompt_forbids_identity_trait_claims():
    """Patterns must be situational, not fixed identity traits."""
    assert "situational and probabilistic" in SUMMARIZER_SYSTEM_PROMPT
    assert "not fixed identity traits" in SUMMARIZER_SYSTEM_PROMPT
