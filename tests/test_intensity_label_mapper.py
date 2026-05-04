"""Unit tests for intensity_label_mapper (spec §B.2.4 / §8.2)."""

from __future__ import annotations

import pytest

from src.app.services.echo.intensity_label_mapper import label_from_score


@pytest.mark.parametrize(
    "score, expected",
    [
        (0.0, "Low"),
        (0.32, "Low"),
        (0.33, "Medium"),
        (0.65, "Medium"),
        (0.66, "High"),
        (1.0, "High"),
    ],
)
def test_label_boundary(score, expected):
    assert label_from_score(score) == expected
