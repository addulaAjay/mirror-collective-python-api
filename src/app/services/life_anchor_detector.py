"""Heuristic Life Anchor detector (MirrorGPT Memory — Phase 2B).

Runs in the chat request path with **no LLM call** — it reuses the
mirror-moment signal the orchestrator already computed plus a small keyword
scan of the user's message. When it trips, the chat response carries a
``memory_prompt`` asking whether to remember the moment as a Life Anchor.

The actual anchor (and the gpt-4o-mini structuring) only happens AFTER the
user confirms, off the hot path — see ``life_anchor_structurer`` and the
``/me/life-anchors/confirm`` endpoint. See docs/MIRRORGPT_MEMORY_PLAN.md
Phase 2.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Keyword → (anchor_type, emotional_weight). First match wins; word-boundary,
# case-insensitive. Ordered most-specific first.
_KEYWORD_RULES: List[Tuple[str, str, str]] = [
    (
        r"\b(passed away|passed on|died|death of|lost (?:my|her|his|our)|funeral)\b",
        "loss",
        "sacred",
    ),
    (r"\b(miscarriage|stillbirth)\b", "loss", "sacred"),
    (r"\b(divorce|divorced|separation|splitting up|split up)\b", "divorce", "high"),
    (
        r"\b(diagnos(?:ed|is)|cancer|terminal|chronic illness|tumou?r)\b",
        "diagnosis",
        "high",
    ),
    (r"\b(sober|sobriety|in recovery|clean (?:for|since))\b", "sobriety", "high"),
    (
        r"\b(gave birth|was born|had (?:a|our) baby|newborn|expecting|pregnant)\b",
        "birth",
        "high",
    ),
    (r"\b(anniversary of)\b", "anniversary", "high"),
    (
        r"\b(moved (?:to|out|away)|new job|quit my job|got married|engaged|"
        r"retired|graduat(?:ed|ion))\b",
        "transition",
        "medium",
    ),
]

# Explicit "remember this" intent → always prompt.
_REMEMBER_INTENT = re.compile(
    r"\b(remember this|don'?t forget this|this matters|mark this|never forget)\b",
    re.IGNORECASE,
)

_PROMPT_TEXT = (
    "This feels like more than a passing reflection. Would you like The Mirror "
    "to remember this as a Life Anchor, so future reflections can hold it with "
    "care?"
)


def detect_life_anchor_candidate(
    user_message: str, result: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Return a ``memory_prompt`` dict when the turn looks anchor-worthy.

    Pure and cheap — a keyword scan plus the already-computed mirror-moment
    signal. No model call and no I/O, so it is safe to run on every chat turn.
    Returns None when nothing looks anchor-worthy.
    """
    text = (user_message or "").strip()
    if not text:
        return None

    anchor_type = "custom"
    emotional_weight = "medium"
    matched = False

    if _REMEMBER_INTENT.search(text):
        matched = True
        anchor_type, emotional_weight = "custom", "high"

    if not matched:
        for pattern, a_type, weight in _KEYWORD_RULES:
            if re.search(pattern, text, re.IGNORECASE):
                matched = True
                anchor_type, emotional_weight = a_type, weight
                break

    # A mirror moment is a significance signal even without keywords. Reuse the
    # value the orchestrator already computed — no new work. (Tunable: this is
    # the knob to dial if beta finds prompts too frequent.)
    if not matched:
        mirror_moment = bool(
            (result.get("change_detection") or {}).get("mirror_moment")
        )
        if mirror_moment:
            matched = True
            anchor_type, emotional_weight = "custom", "medium"

    if not matched:
        return None

    return {
        "prompt": _PROMPT_TEXT,
        "candidate_text": text[:500],
        "anchor_type_guess": anchor_type,
        "emotional_weight_guess": emotional_weight,
    }
