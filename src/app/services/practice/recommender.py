"""Recommender orchestration (spec §9 + §6.3).

Composes the snapshot service, rule matcher, safety filter, cooldown enforcer,
and personalizer to return a single practice recommendation.

V1 ``fallback_enabled`` defaults to True (spec §4.6) — so the spec §12
``NO_RULE_MATCHED`` and ``ALL_CANDIDATES_FILTERED`` errors don't fire under
default config. They're still reachable when the operator flips the flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from ...core.exceptions import (
    AllCandidatesFiltered,
    FallbackOnCooldown,
    LoopNotSupported,
    NoActiveLoops,
    NoRuleMatched,
)
from ...repositories.echo_loop_state_repo import EchoLoopStateRepo
from ...repositories.practice_completion_repo import PracticeCompletionRepo
from ...repositories.reflection_session_repo import ReflectionSessionRepo
from ...repositories.user_personalization_repo import UserPersonalizationRepo
from ..echo.active_loop_filter import filter_active
from ..echo.snapshot_service import V1_SUPPORTED_LOOPS, build_snapshot
from .catalog_loader import Practice, PracticeCatalog, load_practice_catalog
from .cooldown_enforcer import apply as cooldown_apply
from .personalization_loader import (
    PersonalizationDefaults,
    load_personalization_defaults,
)
from .personalizer import score as score_candidates
from .rule_loader import PracticeRulesDoc, load_practice_rules
from .rule_matcher import match as match_rules
from .safety_filter import apply as safety_apply
from .settings_loader import MicroPracticeSettings, load_micro_practice_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PatternInfo:
    loop_id: str
    strength: float
    trend: str
    last_seen: str


@dataclass(frozen=True)
class RecommendResult:
    pattern: PatternInfo
    practice: Practice
    rule_id: str
    private_mode_active: bool


async def recommend(
    *,
    user_id: str,
    session_id: Optional[str],
    selected_loop: Optional[str],
    surface: str,
    sessions_repo: ReflectionSessionRepo,
    loop_state_repo: EchoLoopStateRepo,
    completions_repo: PracticeCompletionRepo,
    prefs_repo: UserPersonalizationRepo,
    now: Optional[datetime] = None,
    # Loaders are injectable for tests (passing None loads the production configs).
    rules_doc: Optional[PracticeRulesDoc] = None,
    catalog: Optional[PracticeCatalog] = None,
    settings: Optional[MicroPracticeSettings] = None,
    defaults: Optional[PersonalizationDefaults] = None,
) -> RecommendResult:
    """Spec §9 algorithm.

    Raises:
        LoopNotSupported: ``selected_loop`` not in V1 set.
        NoActiveLoops: ``selected_loop`` is None and no loop passes the active filter.
        NoRuleMatched / AllCandidatesFiltered: only when ``fallback_enabled=False``.
        FallbackOnCooldown: fallback fired but the fallback practice is on cooldown.
    """
    n = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    rules_doc = rules_doc or load_practice_rules()
    catalog = catalog or load_practice_catalog()
    settings = settings or load_micro_practice_settings()
    defaults = defaults or load_personalization_defaults()

    if selected_loop and selected_loop not in V1_SUPPORTED_LOOPS:
        raise LoopNotSupported(f"loop_id '{selected_loop}' not in V1 set")

    snapshot = await build_snapshot(
        user_id=user_id,
        session_id=session_id,
        sessions_repo=sessions_repo,
        loop_state_repo=loop_state_repo,
    )
    user_tz = snapshot.user_tz
    prefs = await prefs_repo.get_or_default(user_id)

    target = _pick_target(snapshot.loops, selected_loop)

    matched = match_rules(target, rules_doc.rules, now=n)
    if not matched:
        if settings.defaults.fallback_enabled:
            return await _fallback(
                user_id=user_id,
                target=target,
                rules_doc=rules_doc,
                catalog=catalog,
                completions_repo=completions_repo,
                prefs=prefs,
                cooldown_hours=settings.defaults.cooldown_hours_default,
                now=n,
                private_mode=prefs.flags.private_mode,
            )
        raise NoRuleMatched("no rule matched the active loop")

    # Try matched rules in priority desc; first rule with at least one
    # surviving candidate wins.
    matched.sort(key=lambda r: r.priority, reverse=True)
    for rule in matched:
        candidates = [
            catalog.get(pid) for pid in rule.candidates if pid in set(catalog.all_ids())
        ]
        candidates = safety_apply(
            candidates,
            prefs,
            global_disallow_types=defaults.global_config.disallow_types,
        )
        candidates = await cooldown_apply(
            candidates,
            user_id=user_id,
            rule_cooldown_hours=int(rule.cooldown_hours),
            completions_repo=completions_repo,
            now=n,
        )
        if not candidates:
            continue
        scored = score_candidates(candidates, prefs, defaults, user_tz=user_tz, now=n)
        winner = max(scored, key=lambda s: s.score)
        return RecommendResult(
            pattern=PatternInfo(
                loop_id=target.loop_id,
                strength=float(target.intensity_score),
                trend=target.tone_state,
                last_seen=str(target.last_seen),
            ),
            practice=winner.practice,
            rule_id=rule.id,
            private_mode_active=prefs.flags.private_mode,
        )

    # Every matched rule's candidates were dropped by safety/cooldown.
    if settings.defaults.fallback_enabled:
        return await _fallback(
            user_id=user_id,
            target=target,
            rules_doc=rules_doc,
            catalog=catalog,
            completions_repo=completions_repo,
            prefs=prefs,
            cooldown_hours=settings.defaults.cooldown_hours_default,
            now=n,
            private_mode=prefs.flags.private_mode,
        )
    raise AllCandidatesFiltered("all candidate practices filtered by safety/cooldown")


def _pick_target(loops, selected_loop: Optional[str]):
    if selected_loop:
        for l in loops:
            if l.loop_id == selected_loop:
                return l
        # The selected loop must be in the snapshot. If not, treat as "no
        # active loops" (the FE shouldn't have offered it).
        raise NoActiveLoops(f"selected_loop '{selected_loop}' not present in snapshot")
    active = filter_active(loops)
    if not active:
        raise NoActiveLoops("no active loops in snapshot")
    return active[0]  # snapshot loops are pre-sorted by intensity desc


async def _fallback(
    *,
    user_id: str,
    target,
    rules_doc: PracticeRulesDoc,
    catalog: PracticeCatalog,
    completions_repo: PracticeCompletionRepo,
    prefs,
    cooldown_hours: int,
    now: datetime,
    private_mode: bool,
) -> RecommendResult:
    """Spec §9 fallback path.

    Picks the configured default; swaps to the no-breathwork alternate when
    needed; refuses if the chosen fallback is itself on cooldown.
    """
    fb_id = rules_doc.fallback.default_practice_id
    practice = catalog.get(fb_id)
    if prefs.flags.no_breathwork and practice.type == "breath":
        practice = catalog.get(rules_doc.fallback.alternate_for_no_breathwork_id)

    cutoff = now - timedelta(hours=cooldown_hours)
    recent = await completions_repo.list_by_user_since(user_id, cutoff)
    if any(r.practice_id == practice.id for r in recent):
        raise FallbackOnCooldown(
            f"fallback practice '{practice.id}' is within cooldown"
        )

    return RecommendResult(
        pattern=PatternInfo(
            loop_id=target.loop_id,
            strength=float(target.intensity_score),
            trend=target.tone_state,
            last_seen=str(target.last_seen),
        ),
        practice=practice,
        rule_id=rules_doc.fallback.rule_id,  # "fallback"
        private_mode_active=private_mode,
    )
