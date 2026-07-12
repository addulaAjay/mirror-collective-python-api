"""Life Anchor structurer (MirrorGPT Memory — Phase 2B).

A cheap gpt-4o-mini pass that turns a confirmed candidate excerpt into a
structured anchor (type / title / relationship / tone_guidance). Reuses the
conversation_summarizer call + JSON-parse pattern.

This runs ONLY after the user taps "Remember" (on the /confirm endpoint) —
never in the chat hot path. It is best-effort: on any failure the caller falls
back to the heuristic guesses, so confirming an anchor never hard-fails.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from .openai_service import ChatMessage, OpenAIService

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("MIRRORGPT_LIFE_ANCHOR_MODEL", "gpt-4o-mini")
DEFAULT_TEMPERATURE = float(os.getenv("MIRRORGPT_LIFE_ANCHOR_TEMPERATURE", "0.1"))
DEFAULT_MAX_TOKENS = int(os.getenv("MIRRORGPT_LIFE_ANCHOR_MAX_TOKENS", "300"))

_ANCHOR_TYPES = {
    "loss",
    "birth",
    "divorce",
    "diagnosis",
    "sobriety",
    "anniversary",
    "transition",
    "custom",
}
_EMOTIONAL_WEIGHTS = {"sacred", "high", "medium"}

_SYSTEM_PROMPT = """\
You convert a short reflection excerpt the user has chosen to remember into a \
compact, structured "Life Anchor" — a durable fact that should shape future \
reflections with care.

Return ONLY minified JSON with these keys:
{
  "anchor_type": "loss|birth|divorce|diagnosis|sobriety|anniversary|transition|custom",
  "title": "<=100 char neutral, factual title in third person",
  "relationship": "<person relationship if any, else empty string>",
  "emotional_weight": "sacred|high|medium",
  "tone_guidance": ["<=2 short 'do not' guidance lines for how to hold this">]
}

Rules:
- title is a fact, not a feeling ("User's wife passed away", not "Grief").
- emotional_weight: sacred for death/loss; high for divorce, diagnosis, \
sobriety, birth; medium otherwise.
- tone_guidance are gentle guardrails (e.g. "Do not say time heals \
everything."). Empty list is fine.
- Do not invent details not present in the excerpt."""


class LifeAnchorStructurer:
    """gpt-4o-mini structuring of a confirmed Life Anchor candidate."""

    def __init__(
        self,
        openai_service: OpenAIService,
        model: str = DEFAULT_MODEL,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        self.openai_service = openai_service
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def structure(self, candidate_text: str) -> Optional[Dict[str, Any]]:
        """Return a structured anchor dict, or None on any failure.

        Never raises — the confirm endpoint falls back to heuristic guesses.
        """
        text = (candidate_text or "").strip()
        if not text:
            return None

        messages = [
            ChatMessage("system", _SYSTEM_PROMPT),
            ChatMessage("user", text[:2000]),
        ]
        try:
            raw = await self.openai_service.send_with_overrides_async(
                messages=messages,
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as e:  # noqa: BLE001 — best-effort, never break confirm
            logger.warning(f"life-anchor structurer: OpenAI call failed: {e}")
            return None

        return self._parse(raw)

    def _parse(self, raw: str) -> Optional[Dict[str, Any]]:
        """Parse + validate the JSON response. Returns None if unusable."""
        if not raw or not raw.strip():
            return None
        text = raw.strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("life-anchor structurer: malformed JSON")
            return None
        if not isinstance(obj, dict):
            return None

        anchor_type = obj.get("anchor_type")
        title = obj.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        if anchor_type not in _ANCHOR_TYPES:
            anchor_type = "custom"

        emotional_weight = obj.get("emotional_weight")
        if emotional_weight not in _EMOTIONAL_WEIGHTS:
            emotional_weight = "medium"

        relationship = obj.get("relationship")
        relationship = (
            relationship.strip()
            if isinstance(relationship, str) and relationship.strip()
            else None
        )

        tone_in = obj.get("tone_guidance")
        tone_guidance: List[str] = (
            [t.strip() for t in tone_in if isinstance(t, str) and t.strip()][:2]
            if isinstance(tone_in, list)
            else []
        )

        return {
            "anchor_type": anchor_type,
            "title": title.strip()[:200],
            "relationship": relationship,
            "emotional_weight": emotional_weight,
            "tone_guidance": tone_guidance,
        }
