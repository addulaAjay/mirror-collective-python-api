"""Unit tests for active_loop_filter (spec §B.2.3 / §9.1)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.app.services.echo.active_loop_filter import filter_active


@dataclass
class _Row:
    intensity_score: float = 0.0
    tone_state: str = "rising"
    recently_changed: bool = False


@pytest.mark.parametrize(
    "row, expected_in",
    [
        # high + rising → in
        (_Row(intensity_score=0.7, tone_state="rising"), True),
        # high + steady → in
        (_Row(intensity_score=0.7, tone_state="steady"), True),
        # high + softening → in (softening always)
        (_Row(intensity_score=0.7, tone_state="softening"), True),
        # low + softening → in (softening always)
        (_Row(intensity_score=0.2, tone_state="softening"), True),
        # low + rising, no recent change → out
        (_Row(intensity_score=0.2, tone_state="rising", recently_changed=False), False),
        # low + steady, no recent change → out
        (_Row(intensity_score=0.2, tone_state="steady", recently_changed=False), False),
        # low + steady but recently_changed=True → in
        (_Row(intensity_score=0.2, tone_state="steady", recently_changed=True), True),
        # boundary: intensity == 0.60 + rising → in (>= boundary)
        (_Row(intensity_score=0.60, tone_state="rising"), True),
        # boundary: intensity == 0.59 + rising → out
        (_Row(intensity_score=0.59, tone_state="rising"), False),
    ],
)
def test_filter_branches(row, expected_in):
    out = filter_active([row])
    assert (row in out) is expected_in


def test_empty_input_returns_empty():
    assert filter_active([]) == []


def test_preserves_order_of_input():
    a = _Row(intensity_score=0.7, tone_state="rising")
    b = _Row(intensity_score=0.2, tone_state="softening")
    c = _Row(intensity_score=0.7, tone_state="rising")
    out = filter_active([a, b, c])
    assert out == [a, b, c]
