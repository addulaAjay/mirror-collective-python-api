"""In-chat conversational confirm for Life Anchors (MirrorGPT Memory — 2D).

Option B: the whole "remember this?" loop runs inside the chat, with no client
change. When a candidate is detected the ask is appended to MirrorGPT's reply
and a *pending* row is staged; on the next turn a natural-language "yes" is
resolved into a real anchor.

Perf discipline (unchanged): the chat hot path stays LLM-free. On confirm the
anchor is created immediately from the heuristic guesses; the gpt-4o-mini
enrichment (better title / tone_guidance) runs fire-and-forget afterward, and
the anchor is fully usable even if that never completes.

The pending candidate is stored as a ``LifeAnchor`` with ``status="pending"``
and a deterministic SK (``pending#<conversation_id>``) — reusing the anchors
table (no new store) and never surfaced (``list_active_for_user`` requires
``status=="active"``). See docs/MIRRORGPT_MEMORY_PLAN.md Phase 2.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Optional

from ..models.life_anchor import LifeAnchor
from ..repositories.life_anchor_repo import LifeAnchorRepo
from .life_anchor_structurer import LifeAnchorStructurer

logger = logging.getLogger(__name__)

PENDING_STATUS = "pending"

ANCHOR_SAVED_ACK = (
    "I'll hold that with care — I've saved it as a Life Anchor. You can revisit "
    "or remove it anytime."
)

_AFFIRMATIVE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|please|do it|go ahead|"
    r"save (?:it|that|this)|remember (?:it|that|this)|hold (?:it|that))\b",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"\b(no|nope|nah|not now|don'?t|do not|never|skip|leave it|no thanks)\b",
    re.IGNORECASE,
)


def classify_reply(message: str) -> str:
    """Classify a reply to the memory prompt: affirmative | negative | other.

    Pure keyword match — no LLM. 'other' means the user moved on; the caller
    discards the pending candidate and proceeds normally.
    """
    text = (message or "").strip()
    if not text:
        return "other"
    negative = bool(_NEGATIVE.search(text))
    affirmative = bool(_AFFIRMATIVE.search(text))
    if negative and not affirmative:
        return "negative"
    if affirmative and not negative:
        return "affirmative"
    return "other"


def _pending_id(conversation_id: str) -> str:
    return f"pending#{conversation_id}"


def pending_from_candidate(
    user_id: str, conversation_id: str, candidate: Dict[str, Any]
) -> LifeAnchor:
    """Build the staged (unconfirmed) pending row from a detector candidate."""
    text = (candidate.get("candidate_text") or "").strip()
    return LifeAnchor(
        user_id=user_id,
        anchor_id=_pending_id(conversation_id),
        anchor_type=candidate.get("anchor_type_guess", "custom"),
        title=text[:120] or "A moment to remember",
        description=text[:1000],
        emotional_weight=candidate.get("emotional_weight_guess", "medium"),
        status=PENDING_STATUS,
        user_confirmed=False,
        created_from="mirrorgpt",
    )


def _promote(pending: LifeAnchor) -> LifeAnchor:
    """Turn a confirmed pending row into a fresh active anchor (new uuid)."""
    weight = pending.emotional_weight
    return LifeAnchor(
        user_id=pending.user_id,
        # anchor_id left blank → LifeAnchor.__post_init__ assigns a uuid.
        anchor_type=pending.anchor_type,
        title=pending.title,
        description=pending.description,
        emotional_weight=weight,
        reflection_use="always_consider" if weight == "sacred" else "when_relevant",
        status="active",
        user_confirmed=True,
        created_from="mirrorgpt",
    )


async def store_pending(
    repo: LifeAnchorRepo,
    user_id: str,
    conversation_id: str,
    candidate: Dict[str, Any],
) -> None:
    """Stage a pending candidate. Best-effort — never raises into the route."""
    try:
        await repo.upsert(pending_from_candidate(user_id, conversation_id, candidate))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"life-anchor pending store failed for user={user_id}: {e}")


async def resolve_pending(
    repo: LifeAnchorRepo,
    structurer: LifeAnchorStructurer,
    user_id: str,
    conversation_id: str,
    verdict: str,
) -> bool:
    """Resolve a staged pending candidate given the reply's verdict.

    Called only when ``verdict`` is affirmative/negative (the route skips the
    lookup entirely for normal messages, so this costs no read on most turns).
    Returns True iff an active anchor was just created (affirmative). Never
    raises.
    """
    try:
        pending = await repo.get(user_id, _pending_id(conversation_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"life-anchor pending get failed for user={user_id}: {e}")
        return False

    if pending is None or pending.status != PENDING_STATUS:
        return False

    if verdict == "affirmative":
        active = _promote(pending)
        try:
            await repo.upsert(active)
            await repo.delete(user_id, _pending_id(conversation_id))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"life-anchor promote failed for user={user_id}: {e}")
            return False
        schedule_enrichment(repo, structurer, active)
        return True

    # negative / other → discard the candidate.
    try:
        await repo.delete(user_id, _pending_id(conversation_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"life-anchor pending discard failed for user={user_id}: {e}")
    return False


def schedule_enrichment(
    repo: LifeAnchorRepo,
    structurer: LifeAnchorStructurer,
    anchor: LifeAnchor,
) -> None:
    """Fire-and-forget gpt-4o-mini upgrade of a just-saved anchor.

    Off the hot path. The anchor is already usable from heuristic fields; this
    improves the title and adds relationship / tone_guidance when it runs.
    """

    async def _run() -> None:
        try:
            structured = await structurer.structure(anchor.description)
            if not structured:
                return
            anchor.title = structured.get("title") or anchor.title
            if structured.get("anchor_type"):
                anchor.anchor_type = structured["anchor_type"]
            anchor.relationship = structured.get("relationship") or anchor.relationship
            weight = structured.get("emotional_weight")
            if weight:
                anchor.emotional_weight = weight
                if weight == "sacred":
                    anchor.reflection_use = "always_consider"
            if structured.get("tone_guidance"):
                anchor.tone_guidance = structured["tone_guidance"]
            anchor.touch()
            await repo.upsert(anchor)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"life-anchor enrichment failed: {e}")

    try:
        asyncio.create_task(_run())
    except RuntimeError:
        # No running loop (sync contexts) — skip silently.
        logger.debug("life-anchor enrichment: no running event loop; skipping")


def get_pending_anchor_id(conversation_id: str) -> str:
    """Exposed for tests / callers that need the deterministic pending SK."""
    return _pending_id(conversation_id)
