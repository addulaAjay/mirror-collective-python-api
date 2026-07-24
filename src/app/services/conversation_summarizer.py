"""ConversationSummarizer — builds the continuity memory for a conversation.

The summarizer produces a small JSON structure (summary + confidence-tagged
key_themes + open_threads + a nudge signal) and persists it back to the
Conversation record. See docs/MIRRORGPT_CONTINUITY_MEMORY.md for the design
rationale and docs/MIRRORGPT_SUMMARY_V2_PLAN.md for the V2 schema.

This is intentionally a separate service from the main chat path:
- Uses a cheaper model (configurable via MIRRORGPT_SUMMARY_MODEL).
- Uses a dedicated short prompt — NOT the master MirrorGPT prompt — because
  the task is transcript analysis, not response shaping.
- Is safe to call lazily on read OR fire-and-forget on write.
- Failures must never block the chat path. All errors are caught and logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from ..models.conversation import (
    Conversation,
    ConversationMessage,
    KeyTheme,
    normalize_key_themes,
)
from .conversation_service import ConversationService
from .openai_service import ChatMessage, OpenAIService

logger = logging.getLogger(__name__)


# Defaults — overridable via env. Kept here (not in a central config module)
# to match the project's existing per-service env-read pattern.
# Summarize after a single full exchange (1 user + 1 assistant = 2 messages)
# so even short prior chats are recalled. Was 4 (two exchanges), which — with
# the pre-fix message_count undercount — often meant short chats never
# summarized and were never recalled. Overridable via env.
DEFAULT_FIRST_SUMMARY_AT = int(os.getenv("MIRRORGPT_SUMMARY_FIRST_AT", "2"))
DEFAULT_REFRESH_THRESHOLD = int(os.getenv("MIRRORGPT_SUMMARY_REFRESH_THRESHOLD", "6"))
DEFAULT_MODEL = os.getenv("MIRRORGPT_SUMMARY_MODEL", "gpt-4o-mini")
DEFAULT_TEMPERATURE = float(os.getenv("MIRRORGPT_SUMMARY_TEMPERATURE", "0.2"))
DEFAULT_MAX_TOKENS = int(os.getenv("MIRRORGPT_SUMMARY_MAX_TOKENS", "500"))

# How many recent messages we feed into the summarizer prompt at once.
# Bounded so a very long conversation doesn't blow the input context.
MAX_MESSAGES_IN_PROMPT = int(os.getenv("MIRRORGPT_SUMMARY_MAX_MESSAGES", "30"))
MAX_CHARS_PER_TURN = int(os.getenv("MIRRORGPT_SUMMARY_MAX_CHARS_PER_TURN", "1200"))


SUMMARIZER_SYSTEM_PROMPT = """\
You analyze a MirrorGPT conversation transcript and produce a compact
continuity memory the assistant will use to resume future conversations
and determine whether a Reflection Nudge is appropriate.

Your output MUST be a single JSON object with exactly these keys:

{
  "summary": "<1-3 short sentences, grounded plain English, no quoting>",
  "key_themes": [
    {
      "theme": "<short lowercase tag>",
      "confidence": "<high|medium|low>"
    }
  ],
  "open_threads": [
    "<unfinished thing>"
  ],
  "nudge": {
    "eligible": false,
    "reason": "<short grounded reason or empty string>"
  }
}

RULES — these are hard constraints:

- Return ONLY the JSON object. No prose before or after.
- The JSON must be valid and parseable.
- Do not add extra keys.
- If a field has nothing meaningful to include, use an empty list, empty
  string, or false as appropriate.

MEMORY OBJECTIVE

- Capture only the continuity that would materially improve MirrorGPT's next
  conversation with the user.
- Every conversation should preserve a useful conversation anchor, even if no
  meaningful behavioral pattern emerges.
- Focus on behavioral patterns, recurring friction, decision conflicts,
  emotional dynamics, avoidance loops, unresolved situations, or meaningful
  life context that are supported by the conversation.
- Capture likely patterns, not definitive traits.
- Treat all interpretations as probabilistic observations rather than facts.
- Prioritize clarity and continuity over transcript recap.
- Prioritize patterns or conflicts likely to matter across future conversations.
- Do not preserve temporary emotional reactions unless they appear central or
  recurring.
- If the conversation clearly resolves or reverses a previously implied
  pattern, reflect the updated state rather than preserving outdated framing.

SUMMARY RULES

- summary must be grounded, concise, and behavior-oriented when appropriate.
- summary must be a maximum of 3 short sentences.
- If the transcript is thin (one short exchange), keep the summary to a single
  sentence.
- Every summary should provide enough context for MirrorGPT to naturally
  resume the conversation later.
- Do NOT quote the user's words verbatim.
- Do NOT recap the conversation step-by-step.
- Do NOT include filler empathy or assistant phrasing.
- Do NOT include advice, prescriptions, coaching, or suggested actions.
- Treat inferred patterns as situational and probabilistic, not fixed identity traits.
- Prefer wording like:
  - "working through"
  - "showed a pattern of"
  - "seems stuck in"
  rather than definitive personality claims.
- Separate observable events from interpretations whenever possible.
- Do not strengthen an inferred pattern simply because it appeared in a
  previous summary.
- When a previously recurring pattern appears to weaken, resolve, or reverse,
  capture that change explicitly.
- Preserve evidence of growth, not only evidence of recurring friction.
- Do not invent behavioral patterns simply because the conversation was brief
  or factual.
- If no meaningful pattern exists, summarize the useful conversation context
  instead.

GOOD:
"Working through indecision about a career change, with repeated analysis replacing action. Seems conflicted between stability and wanting more autonomy."

GOOD:
"Showed more confidence setting boundaries than in previous conversations, although some hesitation remains."

GOOD:
"Prepared for an upcoming conversation at work and wanted help communicating one concern clearly."

BAD:
"User talked about a meeting with their boss and said they felt stressed."

BAD:
"They are avoidant and afraid of commitment."

KEY_THEMES RULES

- key_themes must contain 0-4 theme objects.
- Each object must contain "theme" and "confidence".
- theme must be a short lowercase behavioral, decision-, or pattern-oriented tag.
- confidence must be one of: high, medium, low.
- Choose themes that are likely to remain useful across future conversations.
- Do not include themes that only describe the topic discussed.
- Prefer underlying behavioral or decision patterns over conversation subjects.
- Prefer specific behavioral tags over broad emotional categories.
- Leave the list empty if no meaningful behavioral theme emerged.

Confidence guidance:

high
- Strongly and explicitly supported throughout the current conversation — the
  user directly confirms it, or it recurs clearly across multiple turns.

medium
- Supported by the current conversation.
- Appears plausible but should be treated as a working hypothesis.

low
- Weak signal.
- Mention only if potentially valuable for future continuity.
- Never use low confidence for speculative personality traits.

GOOD:
{ "theme": "analysis paralysis", "confidence": "high" }
{ "theme": "fear of disappointing others", "confidence": "medium" }
{ "theme": "boundary setting", "confidence": "low" }

OPEN_THREADS RULES

- open_threads must contain 0-3 short phrases.
- Include unresolved decisions, upcoming events, avoided actions, recurring
  conflicts, unfinished conversations, paused reflections, or situations
  likely to continue later.
- Do NOT include vague emotional states.
- Leave the list empty if everything appears resolved.

GOOD:
- "has not decided whether to leave current job"
- "upcoming conversation has not happened yet"
- "avoiding conversation with partner about boundaries"

BAD:
- "feels anxious"
- "still emotional"

NUDGE RULES

- The nudge field exists to help determine whether a future Reflection Nudge
  would be relevant.
- Set eligible to true only when there is a clear, context-supported reason to
  return to the conversation. Appropriate reasons include: unresolved
  decisions, upcoming events, paused reflections, unfinished conversations,
  repeated behavioral patterns, or a user-requested follow-up.
- A conversation may be worth remembering without being worth a Reflection
  Nudge.
- If there is no meaningful reason to proactively re-engage the user, set
  { "eligible": false, "reason": "" }.

GOOD:
{ "eligible": true, "reason": "An important conversation has not happened yet." }
{ "eligible": true, "reason": "A recurring decision conflict remains unresolved." }
{ "eligible": false, "reason": "" }

SAFETY + PRIVACY RULES

- Do NOT include third-party names, addresses, phone numbers, employer names,
  school names, usernames, or identifying details.
- Do NOT include highly sensitive personal data unless absolutely necessary
  for continuity and safety.
- Do NOT diagnose, label, or speculate about mental health conditions,
  personality disorders, attachment styles, trauma disorders, or neurotypes
  unless the user explicitly self-identified them in the transcript.
- Do NOT interpret beyond the evidence in the transcript.
- If the user did not say or strongly imply X, do not claim X.

ANTI-ORACLE RULES

- Do NOT use mystical, spiritual, oracle-like, ceremonial, or theatrical language.
- Banned vocabulary includes:
  sacred, seeker, soul, spirit, divine, cosmic, energetic, field,
  vibration, resonance, awakening, destiny, becoming,
  "wants to emerge", "the mirror remembers".

STYLE RULES

- Use plain English.
- Sound grounded, emotionally intelligent, and believable.
- Keep summaries compact and information-dense.
- Focus on what is most likely to improve the user's next MirrorGPT conversation.
"""


@dataclass
class SummaryResult:
    """Structured output from a summarization run."""

    summary: str
    key_themes: List[KeyTheme]
    open_threads: List[str]
    summarized_through_message_id: str
    summarized_at: str
    nudge_eligible: bool = False
    nudge_reason: str = ""


class ConversationSummarizer:
    """Generates and persists per-conversation continuity summaries."""

    def __init__(
        self,
        openai_service: OpenAIService,
        conversation_service: Optional[ConversationService] = None,
        model: str = DEFAULT_MODEL,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        first_summary_at: int = DEFAULT_FIRST_SUMMARY_AT,
        refresh_threshold: int = DEFAULT_REFRESH_THRESHOLD,
    ):
        self.openai_service = openai_service
        self.conversation_service = conversation_service or ConversationService()
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.first_summary_at = first_summary_at
        self.refresh_threshold = refresh_threshold

    def should_summarize(self, conversation: Conversation) -> bool:
        """Threshold check used by async-on-write trigger.

        - No existing summary AND message_count >= first_summary_at → yes
        - Existing summary but message_count - summarized_at_count >=
          refresh_threshold → yes
        - Otherwise → no
        """
        if conversation.message_count < self.first_summary_at:
            return False
        if not conversation.summary:
            return True
        # We don't track the message_count at summary time directly, but
        # the freshness is approximated by how many messages have been added
        # since summarized_at. Cheap & conservative: if there's been any
        # gap of >= refresh_threshold messages, refresh.
        # NOTE: this is a deliberate simplification — see docs.
        return False  # Refresh logic delegated to summarize_if_stale().

    async def summarize(
        self,
        conversation_id: str,
        user_id: str,
    ) -> Optional[SummaryResult]:
        """Generate a fresh summary for a conversation and persist it.

        Returns None if the conversation cannot be summarized (too few
        messages, model error, malformed JSON, etc). Never raises — this is
        called from fire-and-forget paths.
        """
        try:
            conversation = await self.conversation_service.get_conversation(
                conversation_id, user_id
            )
        except Exception as e:  # noqa: BLE001 — defensive on the async path
            logger.warning(
                "summarize: get_conversation failed for "
                f"conversation_id={conversation_id} user_id={user_id}: {e}"
            )
            return None

        if conversation.message_count < self.first_summary_at:
            logger.debug(
                "summarize: skipping conversation_id=%s — only %d messages, "
                "threshold=%d",
                conversation_id,
                conversation.message_count,
                self.first_summary_at,
            )
            return None

        try:
            messages = await self.conversation_service.get_conversation_history(
                conversation_id=conversation_id,
                user_id=user_id,
                limit=MAX_MESSAGES_IN_PROMPT,
                include_system_messages=False,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "summarize: get_conversation_history failed for "
                f"conversation_id={conversation_id}: {e}"
            )
            return None

        if not messages:
            return None

        transcript = self._build_transcript(messages)
        chat_messages = [
            ChatMessage("system", SUMMARIZER_SYSTEM_PROMPT),
            ChatMessage("user", transcript),
        ]

        try:
            raw = await self.openai_service.send_with_overrides_async(
                messages=chat_messages,
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                # Force valid-JSON output — the schema is now nested
                # (themes carry confidence; nudge is an object).
                response_format={"type": "json_object"},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "summarize: OpenAI call failed for "
                f"conversation_id={conversation_id}: {e}"
            )
            return None

        parsed = self._parse_response(raw)
        if not parsed:
            logger.warning(
                "summarize: malformed summarizer JSON for "
                f"conversation_id={conversation_id}; raw={raw!r}"
            )
            return None

        result = SummaryResult(
            summary=parsed["summary"],
            key_themes=parsed["key_themes"],
            open_threads=parsed["open_threads"],
            summarized_through_message_id=messages[-1].message_id,
            summarized_at=_utc_now_iso(),
            nudge_eligible=parsed["nudge_eligible"],
            nudge_reason=parsed["nudge_reason"],
        )

        await self._persist(conversation, result)
        return result

    async def summarize_if_stale(
        self,
        conversation_id: str,
        user_id: str,
    ) -> Optional[SummaryResult]:
        """Generate a summary only if one is missing or stale.

        Stale is defined as: there have been at least `refresh_threshold`
        new messages since the last summary. Returns the existing summary
        as a SummaryResult if it's fresh enough.
        """
        try:
            conversation = await self.conversation_service.get_conversation(
                conversation_id, user_id
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "summarize_if_stale: get_conversation failed for "
                f"conversation_id={conversation_id}: {e}"
            )
            return None

        if conversation.message_count < self.first_summary_at:
            return None

        # No summary yet — generate.
        if not conversation.summary:
            return await self.summarize(conversation_id, user_id)

        # Check staleness via the summarized_through_message_id marker.
        new_msgs = await self._count_messages_since(
            conversation_id, user_id, conversation.summarized_through_message_id
        )
        if new_msgs >= self.refresh_threshold:
            return await self.summarize(conversation_id, user_id)

        # Fresh — return existing as a SummaryResult.
        return SummaryResult(
            summary=conversation.summary,
            key_themes=conversation.key_themes or [],
            open_threads=conversation.open_threads or [],
            summarized_through_message_id=conversation.summarized_through_message_id
            or "",
            summarized_at=conversation.summarized_at or "",
            nudge_eligible=conversation.nudge_eligible,
            nudge_reason=conversation.nudge_reason,
        )

    async def _count_messages_since(
        self,
        conversation_id: str,
        user_id: str,
        marker_message_id: Optional[str],
    ) -> int:
        """Return how many messages exist after the marker. Marker absent → all."""
        if not marker_message_id:
            return MAX_MESSAGES_IN_PROMPT  # treat as very stale
        try:
            messages = await self.conversation_service.get_conversation_history(
                conversation_id=conversation_id,
                user_id=user_id,
                limit=MAX_MESSAGES_IN_PROMPT,
                include_system_messages=False,
            )
        except Exception:  # noqa: BLE001
            return 0
        # Messages are chronological; count items strictly after marker.
        seen = False
        count = 0
        for msg in messages:
            if seen:
                count += 1
                continue
            if msg.message_id == marker_message_id:
                seen = True
        # Marker not found means it's older than our window — treat as stale.
        if not seen:
            return MAX_MESSAGES_IN_PROMPT
        return count

    def _build_transcript(self, messages: List[ConversationMessage]) -> str:
        """Compose the transcript block fed into the summarizer prompt."""
        lines: List[str] = ["Transcript (chronological):"]
        for msg in messages:
            role = msg.role.upper()
            content = (msg.content or "").strip().replace("\n", " ")
            if len(content) > MAX_CHARS_PER_TURN:
                content = content[:MAX_CHARS_PER_TURN] + "…"
            lines.append(f"{role}: {content}")
        lines.append("")
        lines.append(
            "Produce the JSON object described in the system instructions. "
            "Do not add any prose outside the JSON."
        )
        return "\n".join(lines)

    def _parse_response(self, raw: str) -> Optional[dict]:
        """Strict-ish JSON parse with light recovery for fenced code blocks."""
        if not raw or not raw.strip():
            return None

        text = raw.strip()

        # Strip ```json ... ``` fences if the model added them.
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()

        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None

        if not isinstance(obj, dict):
            return None

        summary = obj.get("summary")
        themes_raw = obj.get("key_themes")
        threads = obj.get("open_threads")

        if not isinstance(summary, str) or not summary.strip():
            return None
        # Accept both V2 object themes ({theme, confidence}) and legacy plain
        # strings; normalize_key_themes drops junk and clamps confidence.
        if not isinstance(themes_raw, list):
            return None
        if not isinstance(threads, list) or not all(
            isinstance(t, str) for t in threads
        ):
            return None

        key_themes = normalize_key_themes(themes_raw)[:4]

        # nudge is optional (V1 output has none) — default to not-eligible. The
        # reason is only meaningful when eligible.
        nudge = obj.get("nudge")
        if not isinstance(nudge, dict):
            nudge = {}
        nudge_eligible = bool(nudge.get("eligible", False))
        nudge_reason = nudge.get("reason", "")
        if not isinstance(nudge_reason, str):
            nudge_reason = ""
        nudge_reason = nudge_reason.strip()
        if not nudge_eligible:
            nudge_reason = ""

        return {
            "summary": summary.strip(),
            "key_themes": key_themes,
            "open_threads": [t.strip() for t in threads if t.strip()][:3],
            "nudge_eligible": nudge_eligible,
            "nudge_reason": nudge_reason,
        }

    async def _persist(self, conversation: Conversation, result: SummaryResult) -> None:
        """Write the summary back onto the Conversation record."""
        conversation.summary = result.summary
        conversation.key_themes = result.key_themes
        conversation.open_threads = result.open_threads
        conversation.nudge_eligible = result.nudge_eligible
        conversation.nudge_reason = result.nudge_reason
        conversation.summarized_through_message_id = (
            result.summarized_through_message_id
        )
        conversation.summarized_at = result.summarized_at

        try:
            await self.conversation_service.update_conversation_summary(conversation)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "summarize: persisting summary failed for "
                f"conversation_id={conversation.conversation_id}: {e}"
            )


def _utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with trailing Z, matching project convention."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
