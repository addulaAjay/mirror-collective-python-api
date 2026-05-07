"""Unit tests for rule_matcher (spec §B.2.5 + §8.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from src.app.services.practice.rule_loader import load_practice_rules
from src.app.services.practice.rule_matcher import match


@dataclass
class _Loop:
    loop_id: str
    intensity_score: float
    tone_state: str
    last_seen: str = field(
        default_factory=lambda: (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _rules():
    return load_practice_rules().rules


def _by_id(rule_id: str):
    for r in _rules():
        if r.id == rule_id:
            return r
    raise KeyError(rule_id)


# ============================================================
# Per-rule matching tables (spec §B.2.5)
# ============================================================


class TestPressureLoopV1:
    def test_matches_at_threshold_rising(self):
        loop = _Loop(loop_id="pressure", intensity_score=0.60, tone_state="rising")
        result = match(loop, [_by_id("pressure_loop_v1")])
        assert len(result) == 1

    def test_matches_above_threshold_steady(self):
        loop = _Loop(loop_id="pressure", intensity_score=0.80, tone_state="steady")
        result = match(loop, [_by_id("pressure_loop_v1")])
        assert len(result) == 1

    def test_below_min_strength_no_match(self):
        loop = _Loop(loop_id="pressure", intensity_score=0.59, tone_state="rising")
        assert match(loop, [_by_id("pressure_loop_v1")]) == []

    def test_softening_excluded(self):
        loop = _Loop(loop_id="pressure", intensity_score=0.80, tone_state="softening")
        assert match(loop, [_by_id("pressure_loop_v1")]) == []


class TestOverwhelmV1:
    def test_matches_at_threshold(self):
        loop = _Loop(loop_id="overwhelm", intensity_score=0.50, tone_state="rising")
        assert len(match(loop, [_by_id("overwhelm_v1")])) == 1

    def test_below_threshold_no_match(self):
        loop = _Loop(loop_id="overwhelm", intensity_score=0.49, tone_state="rising")
        assert match(loop, [_by_id("overwhelm_v1")]) == []


class TestGriefSofteningV1:
    def test_matches_softening_any_score(self):
        loop = _Loop(loop_id="grief", intensity_score=0.10, tone_state="softening")
        assert len(match(loop, [_by_id("grief_softening_v1")])) == 1

    def test_does_not_match_rising(self):
        loop = _Loop(loop_id="grief", intensity_score=0.80, tone_state="rising")
        assert match(loop, [_by_id("grief_softening_v1")]) == []


class TestSelfSilencingV1:
    def test_matches_at_threshold_steady(self):
        loop = _Loop(
            loop_id="self_silencing", intensity_score=0.50, tone_state="steady"
        )
        assert len(match(loop, [_by_id("self_silencing_v1")])) == 1

    def test_below_threshold_no_match(self):
        loop = _Loop(
            loop_id="self_silencing", intensity_score=0.49, tone_state="rising"
        )
        assert match(loop, [_by_id("self_silencing_v1")]) == []


class TestAgencyKeyLowV1:
    def test_matches_at_threshold(self):
        loop = _Loop(loop_id="agency", intensity_score=0.45, tone_state="rising")
        assert len(match(loop, [_by_id("agency_key_low_v1")])) == 1

    def test_below_threshold_no_match(self):
        loop = _Loop(loop_id="agency", intensity_score=0.40, tone_state="rising")
        assert match(loop, [_by_id("agency_key_low_v1")]) == []


class TestTransitionBridgeV1:
    def test_matches_softening_within_window(self):
        loop = _Loop(loop_id="transition", intensity_score=0.50, tone_state="softening")
        assert len(match(loop, [_by_id("transition_bridge_v1")])) == 1

    def test_below_threshold_no_match(self):
        loop = _Loop(loop_id="transition", intensity_score=0.44, tone_state="rising")
        assert match(loop, [_by_id("transition_bridge_v1")]) == []

    def test_outside_recent_days_no_match(self):
        # last_seen older than 3 days from "now" → fails recent_days_max gate.
        old_ts = (
            (datetime.now(timezone.utc) - timedelta(days=10))
            .isoformat()
            .replace("+00:00", "Z")
        )
        loop = _Loop(
            loop_id="transition",
            intensity_score=0.50,
            tone_state="rising",
            last_seen=old_ts,
        )
        assert match(loop, [_by_id("transition_bridge_v1")]) == []


# ============================================================
# Multi-rule
# ============================================================


def test_matching_against_full_rules_returns_only_relevant_rule():
    loop = _Loop(loop_id="pressure", intensity_score=0.7, tone_state="rising")
    rules = _rules()
    matched = match(loop, rules)
    assert {r.id for r in matched} == {"pressure_loop_v1"}


def test_no_match_returns_empty():
    # grief rising — no rule matches grief rising in V1 (the famous spec gap
    # the fallback exists to plug).
    loop = _Loop(loop_id="grief", intensity_score=0.80, tone_state="rising")
    assert match(loop, _rules()) == []
