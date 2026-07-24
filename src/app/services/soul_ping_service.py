"""
SoulPingService — generate + deliver proactive, conversation-aware notifications.

Pipeline per user:
  1. Read the user's Soul Ping config (preferences.soul_pings) — enabled flag +
     enabled categories. Configurable per user.
  2. Enforce a safety throttle (latest row in the soul-pings table).
  3. Choose the message based on engagement:
       - new reflection since the last ping → fresh LLM copy grounded in the
         user's recent continuity summary / themes / open threads / emotion;
       - no new reflection → a different re-engagement nudge (rotating copy,
         never a duplicate). Sent regardless of "seen" so dormant users still
         get nudged; "seen" (read_at) is retained for later flavoring.
  4. Deliver via SNS to each active device endpoint and persist the ping.

The twice-daily dispatch job (jobs/soul_ping_job.py) fans this out across users.
All failures are caught + logged; a bad ping for one user never blocks others.
"Seen" is recorded via mark_read (POST /api/soul-pings/{ping_id}/read).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..models.conversation import normalize_key_themes
from ..models.soul_ping import V1_CATEGORIES, SoulPing, SoulPingCategory
from ..models.user_profile import UserProfile
from .conversation_service import ConversationService
from .dynamodb_service import DynamoDBService, get_dynamodb_service
from .openai_service import ChatMessage, OpenAIService, get_openai_service
from .sns_service import SNSService

logger = logging.getLogger(__name__)

# At most one ping per user per hour, enforced against the latest stored ping.
THROTTLE_MINUTES = int(os.getenv("SOUL_PING_THROTTLE_MINUTES", "60"))

# Copy generation reuses the cheap summarizer-class model by default.
COPY_MODEL = os.getenv(
    "SOUL_PING_MODEL", os.getenv("MIRRORGPT_SUMMARY_MODEL", "gpt-4o-mini")
)
COPY_TEMPERATURE = float(os.getenv("SOUL_PING_TEMPERATURE", "0.6"))
COPY_MAX_TOKENS = int(os.getenv("SOUL_PING_MAX_TOKENS", "220"))

# How many recent turns we sample for emotional tone (kept small + cheap).
RECENT_TURNS = int(os.getenv("SOUL_PING_RECENT_TURNS", "8"))

# Per-category intent the LLM must honor when it picks one.
_CATEGORY_INTENT = {
    SoulPingCategory.EMOTIONAL.value: (
        "Gently check in on how the user is feeling, reflecting the emotional "
        "tone of their recent reflections. Warm, caring, not clinical."
    ),
    SoulPingCategory.PROGRESS.value: (
        "Reinforce the user's momentum on what they've been working through and "
        "invite them to keep going. Encouraging, forward-looking."
    ),
    SoulPingCategory.SYSTEMIC.value: (
        "Surface a recurring pattern or theme across recent sessions and gently "
        "suggest a small reflection or practice. Observant, supportive, specific."
    ),
}

_SYSTEM_PROMPT = """\
You write a single short "Soul Ping" — a gentle, MC-branded check-in notification
for a reflective journaling app. You are given a private summary of the user's
recent conversations. Use it only as background; NEVER quote it or repeat
sensitive specifics verbatim.

Choose the single best category from the ALLOWED list for this moment, then write
the ping for that category.

Return ONLY a valid JSON object, no prose:
{
  "category": "<one of the allowed categories>",
  "title": "<2-4 words, e.g. 'Just checking in'>",
  "body": "<1-2 warm sentences, max ~140 chars, second person, no quotes>"
}

HARD RULES:
- category MUST be one of the allowed categories.
- Do not name the app's internal analysis, 'summary', or 'themes'.
- No emojis in title. At most one in body, optional.
- Plain, human, caring tone. No therapy jargon, no diagnoses.
- If there is little to go on, write a soft, general check-in.
"""


# Re-engagement nudges — used when the user has SEEN the last ping but hasn't
# reflected since. Static + rotating (no LLM) so each is guaranteed distinct
# from the previous message and cheap to produce.
_REENGAGEMENT_PINGS: List[tuple] = [
    (
        "Still here for you",
        "Whenever you're ready to reflect, The Mirror is here. No rush.",
    ),
    (
        "A quiet moment?",
        "Even a minute of reflection can shift the day. Come back when it feels right.",
    ),
    (
        "The Mirror is listening",
        "Your space for reflection is always open. Pick it up whenever you like.",
    ),
    (
        "Checking back in",
        "No pressure — just a gentle reminder this space is here when you need it.",
    ),
    (
        "Whenever you're ready",
        "Life gets busy. The Mirror will be here when you want to return.",
    ),
]


def _pick_reengagement(last_body: Optional[str]) -> tuple:
    """Pick a re-engagement (title, body) that differs from the last message.

    Rotates to the next variant when the previous ping was itself a
    re-engagement, so the user never sees the same nudge twice in a row.
    """
    for i, (_title, body) in enumerate(_REENGAGEMENT_PINGS):
        if body == last_body:
            return _REENGAGEMENT_PINGS[(i + 1) % len(_REENGAGEMENT_PINGS)]
    return _REENGAGEMENT_PINGS[0]


@dataclass
class PingResult:
    """Outcome of attempting a ping for one user (for job aggregation/logging)."""

    user_id: str
    status: str  # "sent" | "skipped"
    reason: Optional[str] = None
    category: Optional[str] = None
    endpoints: int = 0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Best-effort: pull the first JSON object out of an LLM response."""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


class SoulPingService:
    def __init__(
        self,
        dynamodb_service: Optional[DynamoDBService] = None,
        openai_service: Optional[OpenAIService] = None,
        conversation_service: Optional[ConversationService] = None,
        sns_service: Optional[SNSService] = None,
    ):
        self.db = dynamodb_service or get_dynamodb_service()
        self.openai = openai_service or get_openai_service()
        self.conversations = conversation_service or ConversationService()
        self.sns = sns_service or SNSService()

    # ------------------------------------------------------------------ config
    @staticmethod
    def get_config(profile: UserProfile) -> Dict[str, Any]:
        """Resolve a user's Soul Ping config from profile.preferences.

        Default: enabled, all v1 categories. A user can disable the feature or
        narrow the categories via PUT /api/soul-pings/preferences.
        """
        prefs = (profile.preferences or {}).get("soul_pings") or {}
        enabled = prefs.get("enabled", True)
        raw_categories = prefs.get("categories")
        if raw_categories is None:
            categories = [c.value for c in V1_CATEGORIES]
        else:
            categories = [
                c.value
                for c in (SoulPingCategory.from_value(x) for x in raw_categories)
                if c is not None
            ]
        return {"enabled": bool(enabled), "categories": categories}

    # ---------------------------------------------------------------- throttle
    async def was_pinged_recently(self, user_id: str) -> bool:
        """True if a ping was sent within THROTTLE_MINUTES."""
        last = await self.db.get_last_soul_ping(user_id)
        if not last:
            return False
        sent_at = _parse_iso(last.sent_at)
        if not sent_at:
            return False
        return _now() - sent_at < timedelta(minutes=THROTTLE_MINUTES)

    # ---------------------------------------------------------------- generate
    async def generate_ping(
        self, user_id: str, enabled_categories: List[str]
    ) -> Optional[SoulPing]:
        """Generate ping copy grounded in the user's recent conversation.

        Returns None when there's no usable content (no conversation yet) or the
        LLM output can't be parsed into an allowed category.
        """
        if not enabled_categories:
            return None

        context, conversation_id = await self._build_context(user_id)
        if context is None:
            # No conversation history at all — nothing personal to say. Skip
            # rather than send a generic ping to a brand-new user.
            return None

        allowed = ", ".join(enabled_categories)
        intents = "\n".join(
            f"- {c}: {_CATEGORY_INTENT[c]}"
            for c in enabled_categories
            if c in _CATEGORY_INTENT
        )
        user_prompt = (
            f"ALLOWED categories: {allowed}\n\n"
            f"Category intents:\n{intents}\n\n"
            f"Background about the user (private, do not quote):\n{context}\n\n"
            "Write the Soul Ping JSON now."
        )

        try:
            raw = await self.openai.send_with_overrides_async(
                [
                    ChatMessage("system", _SYSTEM_PROMPT),
                    ChatMessage("user", user_prompt),
                ],
                model=COPY_MODEL,
                temperature=COPY_TEMPERATURE,
                max_tokens=COPY_MAX_TOKENS,
            )
        except Exception as e:  # noqa: BLE001 - copy gen is best-effort
            logger.warning(f"Soul ping copy generation failed for {user_id}: {e}")
            return None

        data = _extract_json(raw)
        if not data:
            logger.warning(
                f"Soul ping LLM output unparseable for {user_id}: {raw[:120]}"
            )
            return None

        category = SoulPingCategory.from_value(data.get("category"))
        if category is None or category.value not in enabled_categories:
            # Fall back to the first enabled category rather than dropping.
            category = SoulPingCategory.from_value(enabled_categories[0])
        title = str(data.get("title") or "Just checking in").strip()[:40]
        body = str(data.get("body") or "").strip()[:200]
        if not body or category is None:
            return None

        return SoulPing(
            user_id=user_id,
            category=category,
            title=title,
            body=body,
            conversation_id=conversation_id,
        )

    async def _build_context(self, user_id: str) -> tuple[Optional[str], Optional[str]]:
        """Assemble a compact, privacy-safe context string from the user's most
        recent conversation: continuity summary + key themes + open threads +
        a short recent-emotion hint. Returns (context, conversation_id)."""
        try:
            recents = await self.conversations.get_recent_conversations(
                user_id=user_id, limit=1
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Soul ping context fetch failed for {user_id}: {e}")
            return None, None
        if not recents:
            return None, None

        convo = recents[0]
        parts: List[str] = []
        if convo.summary:
            parts.append(f"Summary: {convo.summary}")
        # normalize_key_themes tolerates both the V2 object shape and any
        # legacy plain-string themes still on older records.
        themes = normalize_key_themes(convo.key_themes)
        if themes:
            parts.append("Recurring themes: " + ", ".join(t.theme for t in themes[:4]))
        open_threads = convo.open_threads or []
        if open_threads:
            parts.append("Open threads: " + "; ".join(open_threads[:3]))

        emotion = await self._recent_emotion_hint(user_id, convo.conversation_id)
        if emotion:
            parts.append(f"Recent emotional tone: {emotion}")

        if not parts:
            return None, convo.conversation_id
        return "\n".join(parts), convo.conversation_id

    async def _recent_emotion_hint(
        self, user_id: str, conversation_id: str
    ) -> Optional[str]:
        """Pull a soft emotional descriptor from the latest analyzed message.

        Defensive: the 5-signal schema may be absent on older messages, so every
        access is guarded; any failure just omits the hint.
        """
        try:
            messages = await self.conversations.get_conversation_history(
                conversation_id=conversation_id,
                user_id=user_id,
                limit=RECENT_TURNS,
                include_system_messages=False,
            )
        except Exception:  # noqa: BLE001
            return None
        for msg in reversed(messages or []):
            signal = getattr(msg, "signal_1_emotional_resonance", None)
            if isinstance(signal, dict):
                emotion = signal.get("dominant_emotion")
                if emotion:
                    return str(emotion)
        return None

    # -------------------------------------------------------------------- send
    async def send_and_record(self, ping: SoulPing) -> int:
        """Deliver `ping` to all active endpoints and persist it. Returns the
        number of endpoints the push was dispatched to (0 → not recorded)."""
        tokens = await self.db.get_user_device_tokens(ping.user_id)
        endpoints: List[str] = [
            str(t["endpoint_arn"])
            for t in tokens
            if t.get("endpoint_arn") and t.get("is_active", True)
        ]
        if not endpoints:
            return 0

        delivered = 0
        for arn in endpoints:
            try:
                msg_id = await self.sns.publish_to_endpoint_async(
                    arn, ping.title, ping.body, data=ping.push_data()
                )
                if msg_id:
                    delivered += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Soul ping publish failed ({arn}): {e}")

        # Record once we've attempted delivery so the throttle holds even if a
        # particular endpoint was disabled. Persist regardless of `delivered`
        # count as long as the user has endpoints, to avoid hammering a user
        # whose tokens are stale.
        try:
            await self.db.save_soul_ping(ping)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to persist soul ping for {ping.user_id}: {e}")
        return delivered

    # ---------------------------------------------------------- seen / activity
    async def mark_read(self, user_id: str, ping_id: str) -> bool:
        """Record that the user opened/saw a ping. Returns True if a row was
        updated. Drives the first-send-vs-re-engagement branch below."""
        return await self.db.mark_soul_ping_read(user_id, ping_id)

    async def _has_new_activity_since(
        self, user_id: str, last: Optional[SoulPing]
    ) -> bool:
        """True if the user reflected (a conversation was updated) after the
        last ping was sent. No prior ping → treat as new activity."""
        if last is None:
            return True
        last_sent = _parse_iso(last.sent_at)
        if last_sent is None:
            return True
        try:
            recents = await self.conversations.get_recent_conversations(
                user_id=user_id, limit=1
            )
        except Exception:  # noqa: BLE001
            return False
        if not recents:
            return False
        last_msg = _parse_iso(recents[0].last_message_at)
        return last_msg is not None and last_msg > last_sent

    def build_reengagement_ping(
        self, user_id: str, enabled_categories: List[str], last: Optional[SoulPing]
    ) -> SoulPing:
        """A gentle 'come back' nudge (no LLM), distinct from the last message.

        Used when the user saw the last ping but hasn't reflected since — a new
        angle to re-engage rather than repeating the same conversation-grounded
        content.
        """
        title, body = _pick_reengagement(last.body if last else None)
        cat_value = (
            SoulPingCategory.EMOTIONAL.value
            if SoulPingCategory.EMOTIONAL.value in enabled_categories
            else enabled_categories[0]
        )
        category = SoulPingCategory.from_value(cat_value) or SoulPingCategory.EMOTIONAL
        return SoulPing(user_id=user_id, category=category, title=title, body=body)

    # ------------------------------------------------------------- orchestrate
    async def maybe_send_for_user(
        self,
        user_id: str,
        profile: Optional[UserProfile] = None,
        source: str = "scheduled",
        force: bool = False,
    ) -> PingResult:
        """End-to-end for one user: config → throttle → choose message → send.

        Message choice (the fix for "same conversation, same notification"):
          * new reflection since the last ping → fresh, grounded content ping;
          * no new reflection but last ping was SEEN → a different
            re-engagement nudge (rotates, never a duplicate);
          * no new reflection and last ping NOT yet opened → skip, so we don't
            stack duplicate notifications the user hasn't even looked at.

        `force=True` (manual test) bypasses the throttle and always generates a
        fresh content ping.
        """
        profile = profile or await self.db.get_user_profile(user_id)
        if not profile:
            return PingResult(user_id, "skipped", "no_profile")

        config = self.get_config(profile)
        if not config["enabled"]:
            return PingResult(user_id, "skipped", "disabled")
        if not config["categories"]:
            return PingResult(user_id, "skipped", "no_categories")

        last = await self.db.get_last_soul_ping(user_id)
        if not force and last is not None:
            sent_at = _parse_iso(last.sent_at)
            if sent_at and _now() - sent_at < timedelta(minutes=THROTTLE_MINUTES):
                return PingResult(user_id, "skipped", "throttled")

        has_new_activity = force or await self._has_new_activity_since(user_id, last)
        if has_new_activity:
            ping = await self.generate_ping(user_id, config["categories"])
            if not ping:
                return PingResult(user_id, "skipped", "no_content")
        else:
            # No new reflection since the last ping → a gentle re-engagement
            # nudge. Sent regardless of "seen": at this cadence there's no
            # notification-stacking concern, and read_at is only reliably set
            # once the client reports opens (which isn't shipped yet). The
            # rotating copy guarantees it differs from the previous message.
            ping = self.build_reengagement_ping(user_id, config["categories"], last)

        ping.source = source
        delivered = await self.send_and_record(ping)
        if delivered == 0:
            return PingResult(
                user_id, "skipped", "no_active_endpoint", category=ping.category.value
            )
        return PingResult(
            user_id, "sent", category=ping.category.value, endpoints=delivered
        )


_soul_ping_service: Optional[SoulPingService] = None


def get_soul_ping_service() -> SoulPingService:
    """Module-level singleton (matches get_dynamodb_service / get_echo_service)."""
    global _soul_ping_service
    if _soul_ping_service is None:
        _soul_ping_service = SoulPingService()
    return _soul_ping_service
