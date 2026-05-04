"""Reflection Room V1 — config validation script.

Loads each of the 8 Reflection Room config files via its real loader and
asserts the cross-file invariants. Performs no DDB writes — purpose is to
fail fast in CI before deploy if a YAML/JSON file is broken.

Exit codes:
    0  All configs loaded and cross-checked OK
    1  Any loader raised, or any cross-check failed

Usage:
    python scripts/seed_reflection_config.py
"""

from __future__ import annotations

import logging
import os
import sys

# Ensure we can import src.app.* when run from repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("seed_reflection_config")


def _check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        logger.info(f"OK   {label}")
    else:
        msg = f"FAIL {label}"
        if detail:
            msg += f" — {detail}"
        logger.error(msg)
        raise SystemExit(1)


def main() -> int:
    from src.app.services.echo.tone_library_loader import load_tone_library
    from src.app.services.practice.catalog_loader import load_practice_catalog
    from src.app.services.practice.personalization_loader import (
        load_personalization_defaults,
    )
    from src.app.services.practice.rule_loader import load_practice_rules
    from src.app.services.practice.settings_loader import load_micro_practice_settings
    from src.app.services.reflection.motif_mapping_loader import load_motif_mapping
    from src.app.services.reflection.quiz_rules_loader import load_quiz_rules
    from src.app.services.reflection.quiz_to_loop_seeding_loader import (
        SUPPORTED_LOOPS,
        SUPPORTED_TONES,
        load_quiz_to_loop_seeding,
    )

    qr = load_quiz_rules()
    _check(
        "quiz_rules: 4 questions present", set(qr.questions) == {"q1", "q2", "q3", "q4"}
    )
    _check(
        "quiz_rules: weights match q1/q2/q3/q4=1/2/2/1",
        qr.weights == {"q1": 1, "q2": 2, "q3": 2, "q4": 1},
    )

    mm = load_motif_mapping()
    _check("motif_mapping: 11 tags", len(mm.motifs) == 11, f"got {len(mm.motifs)}")
    motif_ids = [e.motif_id for e in mm.all_entries()]
    _check("motif_mapping: motif_ids unique", len(set(motif_ids)) == len(motif_ids))

    # Every Q3 answer in the quiz rules must map to a tag we have a motif for —
    # this is a load-bearing invariant for the tie-break path (§7).
    quiz_tags = set()
    for q in qr.questions.values():
        for tags in q.answers.values():
            for t in tags:
                quiz_tags.add(t)
    motif_tag_set = set(mm.motifs.keys())
    missing = quiz_tags - motif_tag_set
    _check(
        "quiz_rules: every answer-tag has a motif row",
        not missing,
        f"missing tags: {sorted(missing)}",
    )

    qs = load_quiz_to_loop_seeding()
    _check("quiz_to_loop_seeding: top_n=3", qs.config.top_n == 3)
    _check(
        "quiz_to_loop_seeding: every contribution loop is in V1 supported set",
        all(
            c.loop in SUPPORTED_LOOPS
            for q in qs.contributions.values()
            for ans in q.answers.values()
            for c in ans
        ),
    )
    _check(
        "quiz_to_loop_seeding: every contribution tone is in V1 supported set",
        all(
            c.tone in SUPPORTED_TONES
            for q in qs.contributions.values()
            for ans in q.answers.values()
            for c in ans
        ),
    )

    tl = load_tone_library()
    _check("tone_library: 6 loops", len(tl.loops) == 6)
    pairs = sum(len(b.tones) for b in tl.loops.values())
    _check("tone_library: 18 (loop, tone) pairs", pairs == 18, f"got {pairs}")

    pr = load_practice_rules()
    _check("practice_rules: 6 rules", len(pr.rules) == 6, f"got {len(pr.rules)}")
    rule_ids = [r.id for r in pr.rules]
    _check("practice_rules: rule ids unique", len(set(rule_ids)) == len(rule_ids))
    rule_loops = {r.when.loop_id for r in pr.rules}
    _check(
        "practice_rules: rules cover all 6 V1 loops",
        rule_loops == set(SUPPORTED_LOOPS),
        f"got {sorted(rule_loops)}",
    )

    pc = load_practice_catalog()
    _check(
        "practice_catalog: 17 practices",
        len(pc.practices) == 17,
        f"got {len(pc.practices)}",
    )
    catalog_ids = set(pc.all_ids())
    for r in pr.rules:
        for cid in r.candidates:
            _check(
                f"practice_rules: rule '{r.id}' candidate '{cid}' in catalog",
                cid in catalog_ids,
            )
    _check(
        f"practice_rules: fallback default '{pr.fallback.default_practice_id}' in catalog",
        pr.fallback.default_practice_id in catalog_ids,
    )
    _check(
        f"practice_rules: fallback alternate "
        f"'{pr.fallback.alternate_for_no_breathwork_id}' in catalog",
        pr.fallback.alternate_for_no_breathwork_id in catalog_ids,
    )

    s = load_micro_practice_settings()
    _check(
        "micro_practice.settings: cooldown_hours_default=12 grief=24",
        s.defaults.cooldown_hours_default == 12
        and s.defaults.cooldown_hours_grief == 24,
    )

    pd = load_personalization_defaults()
    _check(
        "personalization.defaults: 4 time_of_day buckets",
        set(pd.time_of_day_buckets.keys()) == {"morning", "midday", "evening", "night"},
    )

    logger.info("Reflection Room V1 config: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
