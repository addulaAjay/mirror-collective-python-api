"""Unit tests for personalizer (spec §B.2.8 + §9.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.app.models.user_personalization import (
    HelpfulnessEvent,
    RecentUseEntry,
    UserPersonalization,
)
from src.app.services.practice.catalog_loader import load_practice_catalog
from src.app.services.practice.personalization_loader import (
    load_personalization_defaults,
)
from src.app.services.practice.personalizer import score

NOW = datetime(2026, 5, 3, 14, 0, tzinfo=timezone.utc)  # 10:00 EDT (morning bucket)


@pytest.fixture
def defaults():
    return load_personalization_defaults()


@pytest.fixture
def catalog():
    return load_practice_catalog()


def _ts(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def _hours_ago_ts(hours_ago: int) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


# ============================================================
# Helpfulness
# ============================================================


class TestHelpfulnessVotes:
    def test_helpful_today_adds_2(self, defaults, catalog):
        prefs = UserPersonalization(
            user_id="u1",
            practice_helpfulness={
                "breath_4_6": [HelpfulnessEvent(ts=_ts(0), helpful=True)]
            },
        )
        result = score(
            [catalog.get("breath_4_6")], prefs, defaults, user_tz="UTC", now=NOW
        )
        assert result[0].score == pytest.approx(2.0, abs=1e-3)

    def test_not_helpful_today_subtracts_2(self, defaults, catalog):
        prefs = UserPersonalization(
            user_id="u1",
            practice_helpfulness={
                "breath_4_6": [HelpfulnessEvent(ts=_ts(0), helpful=False)]
            },
        )
        result = score(
            [catalog.get("breath_4_6")], prefs, defaults, user_tz="UTC", now=NOW
        )
        assert result[0].score == pytest.approx(-2.0, abs=1e-3)

    def test_decay_at_half_life_halves_score(self, defaults, catalog):
        # 21-day half-life → vote 21d ago decays to 0.5 weight → +1.0.
        prefs = UserPersonalization(
            user_id="u1",
            practice_helpfulness={
                "breath_4_6": [HelpfulnessEvent(ts=_ts(21), helpful=True)]
            },
        )
        result = score(
            [catalog.get("breath_4_6")], prefs, defaults, user_tz="UTC", now=NOW
        )
        assert result[0].score == pytest.approx(1.0, abs=1e-2)


# ============================================================
# Time of day
# ============================================================


class TestTimeOfDay:
    def test_match_adds_0_5(self, defaults, catalog):
        # NOW = 14:00 UTC = 10:00 EDT → "morning" bucket [5, 11).
        prefs = UserPersonalization(
            user_id="u1",
            time_of_day_history={"morning": 10, "evening": 1},
        )
        result = score(
            [catalog.get("breath_4_6")],
            prefs,
            defaults,
            user_tz="America/New_York",
            now=NOW,
        )
        assert result[0].score == pytest.approx(0.5, abs=1e-3)

    def test_no_history_no_boost(self, defaults, catalog):
        prefs = UserPersonalization(user_id="u1")
        result = score(
            [catalog.get("breath_4_6")],
            prefs,
            defaults,
            user_tz="America/New_York",
            now=NOW,
        )
        assert result[0].score == 0.0

    def test_different_bucket_no_boost(self, defaults, catalog):
        prefs = UserPersonalization(
            user_id="u1",
            # Most-common = evening, but we're in morning now → no boost.
            time_of_day_history={"morning": 1, "evening": 10},
        )
        result = score(
            [catalog.get("breath_4_6")],
            prefs,
            defaults,
            user_tz="America/New_York",
            now=NOW,
        )
        assert result[0].score == 0.0


# ============================================================
# Recent use penalty
# ============================================================


class TestRecentUsePenalty:
    def test_used_within_24h_penalizes(self, defaults, catalog):
        prefs = UserPersonalization(
            user_id="u1",
            recent_use={
                "breath_4_6": RecentUseEntry(last_used_at=_hours_ago_ts(1), count_30d=1)
            },
        )
        result = score(
            [catalog.get("breath_4_6")], prefs, defaults, user_tz="UTC", now=NOW
        )
        assert result[0].score == pytest.approx(-1.0, abs=1e-3)

    def test_used_outside_24h_no_penalty(self, defaults, catalog):
        prefs = UserPersonalization(
            user_id="u1",
            recent_use={
                "breath_4_6": RecentUseEntry(
                    last_used_at=_hours_ago_ts(25), count_30d=1
                )
            },
        )
        result = score(
            [catalog.get("breath_4_6")], prefs, defaults, user_tz="UTC", now=NOW
        )
        assert result[0].score == 0.0


# ============================================================
# Combined
# ============================================================


def test_combined_helpful_today_plus_recent_use(defaults, catalog):
    prefs = UserPersonalization(
        user_id="u1",
        practice_helpfulness={
            "breath_4_6": [HelpfulnessEvent(ts=_ts(0), helpful=True)]
        },
        recent_use={
            "breath_4_6": RecentUseEntry(last_used_at=_hours_ago_ts(1), count_30d=1)
        },
    )
    result = score([catalog.get("breath_4_6")], prefs, defaults, user_tz="UTC", now=NOW)
    # +2.0 helpful, -1.0 recent use → +1.0
    assert result[0].score == pytest.approx(1.0, abs=1e-3)


def test_returns_one_scored_per_candidate_in_order(defaults, catalog):
    prefs = UserPersonalization(user_id="u1")
    candidates = [catalog.get("breath_4_6"), catalog.get("name_and_need")]
    result = score(candidates, prefs, defaults, user_tz="UTC", now=NOW)
    assert len(result) == 2
    assert result[0].practice.id == "breath_4_6"
    assert result[1].practice.id == "name_and_need"
