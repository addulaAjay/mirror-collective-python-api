"""ConversationSummarizer — builds the continuity memory for a conversation.

The summarizer produces a small JSON structure (summary + key_themes +
open_threads) and persists it back to the Conversation record. See
docs/MIRRORGPT_CONTINUITY_MEMORY.md for the design rationale.

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

from ..models.conversation import Conversation, ConversationMessage
from .conversation_service import ConversationService
from .openai_service import ChatMessage, OpenAIService

logger = logging.getLogger(__name__)


# Defaults — overridable via env. Kept here (not in a central config module)
# to match the project's existing per-service env-read pattern.
DEFAULT_FIRST_SUMMARY_AT = int(os.getenv("MIRRORGPT_SUMMARY_FIRST_AT", "4"))
DEFAULT_REFRESH_THRESHOLD = int(os.getenv("MIRRORGPT_SUMMARY_REFRESH_THRESHOLD", "6"))
DEFAULT_MODEL = os.getenv("MIRRORGPT_SUMMARY_MODEL", "gpt-4o-mini")
DEFAULT_TEMPERATURE = float(os.getenv("MIRRORGPT_SUMMARY_TEMPERATURE", "0.2"))
DEFAULT_MAX_TOKENS = int(os.getenv("MIRRORGPT_SUMMARY_MAX_TOKENS", "400"))

# How many recent messages we feed into the summarizer prompt at once.
# Bounded so a very long conversation doesn't blow the input context.
MAX_MESSAGES_IN_PROMPT = int(os.getenv("MIRRORGPT_SUMMARY_MAX_MESSAGES", "30"))
MAX_CHARS_PER_TURN = int(os.getenv("MIRRORGPT_SUMMARY_MAX_CHARS_PER_TURN", "1200"))


SUMMARIZER_SYSTEM_PROMPT = """\
You analyze a MirrorGPT conversation transcript and produce a compact
continuity memory the assistant will use to pick up the thread later.

Your output MUST be a single JSON object with exactly these keys:
{
  "summary": "<1-3 short sentences, grounded plain English, no quoting>",
  "key_themes": ["<short tag>", "<short tag>"],
  "open_threads": ["<unfinished thing>", "<unfinished thing>"]
}

RULES — these are hard constraints:

- Return ONLY the JSON object. No prose before or after.
- The JSON must be valid and parseable.
- Do not add extra keys.
- If a field has nothing meaningful to include, use an empty list or a very
  short neutral summary.

MEMORY OBJECTIVE

- Capture continuity that would help MirrorGPT resume the conversation later.
- Focus on behavioral patterns, recurring friction, decision conflicts,
  emotional dynamics, avoidance loops, or unresolved situations.
- Prioritize clarity and continuity over transcript recap.
- Prioritize patterns or conflicts likely to matter across future conversations.
- Do not preserve temporary emotional reactions unless they appear central
  or recurring.
- If the conversation clearly resolves or reverses a previously implied
  pattern, reflect the updated state rather than preserving outdated framing.

SUMMARY RULES

- summary must be grounded, concise, and behavior-oriented.
- summary must be maximum 3 short sentences.
- If the transcript is thin (one short exchange), keep summary to a single sentence.
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
- Separate observable events from interpretations when possible.

GOOD:
"Working through indecision about a career change, with repeated analysis replacing action. Seems conflicted between stability and wanting more autonomy."

BAD:
"User talked about a meeting with their boss and said they felt stressed."

BAD:
"They are avoidant and afraid of commitment."

KEY_THEMES RULES

- key_themes must contain 1-4 short lowercase tags.
- Tags should be behavior-, decision-, or pattern-oriented.
- Prefer specific behavioral tags over broad emotional categories.
- Empty list if nothing clearly emerged.

GOOD TAGS:
- "conflict avoidance"
- "people-pleasing"
- "analysis paralysis"
- "fear of disappointing others"
- "career indecision"

BAD TAGS:
- "stress"
- "sadness"
- "relationships"
- "life"

OPEN_THREADS RULES

- open_threads must contain 0-3 short phrases.
- Include unresolved decisions, avoided actions, recurring conflicts,
  unfinished conversations, or situations likely to continue later.
- Do NOT include vague emotional states.
- Empty list if everything appears resolved.

GOOD:
- "has not decided whether to leave current job"
- "avoiding conversation with partner about boundaries"

BAD:
- "feels anxious"
- "still emotional"

SAFETY + PRIVACY RULES

- Do NOT include third-party names, addresses, phone numbers, employer names,
  school names, usernames, or identifying details.
- Do NOT include highly sensitive personal data unless absolutely necessary
  for continuity and safety.
- Do NOT diagnose, label, or speculate about mental health conditions,
  personality disorders, attachment styles, trauma disorders, or neurotypes
  unless the user explicitly self-identified them in the transcript.
- Do NOT interpret beyond evidence in the transcript.
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
- Focus on what is likely to matter in the next conversation.
"""


@dataclass
class SummaryResult:
    """Structured output from a summarization run."""

    summary: str
    key_themes: List[str]
    open_threads: List[str]
    summarized_through_message_id: str
    summarized_at: str


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
        themes = obj.get("key_themes")
        threads = obj.get("open_threads")

        if not isinstance(summary, str) or not summary.strip():
            return None
        if not isinstance(themes, list) or not all(isinstance(t, str) for t in themes):
            return None
        if not isinstance(threads, list) or not all(
            isinstance(t, str) for t in threads
        ):
            return None

        return {
            "summary": summary.strip(),
            "key_themes": [t.strip() for t in themes if t.strip()][:4],
            "open_threads": [t.strip() for t in threads if t.strip()][:3],
        }

    async def _persist(self, conversation: Conversation, result: SummaryResult) -> None:
        """Write the summary back onto the Conversation record."""
        conversation.summary = result.summary
        conversation.key_themes = result.key_themes
        conversation.open_threads = result.open_threads
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
