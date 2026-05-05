"""Intensity score → ``High`` / ``Medium`` / ``Low`` (spec §8.2).

Pure function; band boundaries are inclusive on the lower edge (per the spec
test fixture in `02_TASK_BREAKDOWN_AND_TESTS.md` §B.2.4: `0.33` → Medium,
`0.66` → High).
"""

from __future__ import annotations


def label_from_score(score: float) -> str:
    if score >= 0.66:
        return "High"
    if score >= 0.33:
        return "Medium"
    return "Low"
