"""Unit tests for session_lifecycle (spec §3.1, §6.1, §8.3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.app.models.reflection_session import ReflectionSession
from src.app.services.reflection.session_lifecycle import (
    DEFAULT_TZ_FALLBACK,
    compute_session_window,
    is_active,
    iso,
    next_midnight_in_tz,
    parse_iso,
    resolve_user_tz,
)


class TestResolveUserTz:
    def test_valid_header_wins(self, monkeypatch):
        monkeypatch.delenv("REFLECTION_DEFAULT_USER_TZ", raising=False)
        assert resolve_user_tz("Asia/Tokyo") == "Asia/Tokyo"

    def test_invalid_header_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("REFLECTION_DEFAULT_USER_TZ", "America/Chicago")
        assert resolve_user_tz("Mars/OlympusMons") == "America/Chicago"

    def test_no_header_uses_env_default(self, monkeypatch):
        monkeypatch.setenv("REFLECTION_DEFAULT_USER_TZ", "America/Chicago")
        assert resolve_user_tz(None) == "America/Chicago"

    def test_no_header_no_env_uses_hard_fallback(self, monkeypatch):
        monkeypatch.delenv("REFLECTION_DEFAULT_USER_TZ", raising=False)
        assert resolve_user_tz(None) == DEFAULT_TZ_FALLBACK
        assert DEFAULT_TZ_FALLBACK == "America/New_York"


class TestNextMidnightInTz:
    def test_la_at_11pm_utc_yields_4am_utc_tomorrow(self):
        # 23:00 UTC on 2026-05-03 = 16:00 PDT same day → next LA midnight is
        # 2026-05-04 00:00 PDT = 2026-05-04 07:00 UTC.
        now = datetime(2026, 5, 3, 23, 0, 0, tzinfo=timezone.utc)
        midnight = next_midnight_in_tz(now, "America/Los_Angeles")
        assert midnight == datetime(2026, 5, 4, 7, 0, 0, tzinfo=timezone.utc)

    def test_ny_at_11pm_utc_yields_4am_utc_next_day(self):
        # 23:00 UTC = 19:00 EDT same day → next NY midnight = 04:00 UTC tomorrow.
        now = datetime(2026, 5, 3, 23, 0, 0, tzinfo=timezone.utc)
        midnight = next_midnight_in_tz(now, "America/New_York")
        assert midnight == datetime(2026, 5, 4, 4, 0, 0, tzinfo=timezone.utc)

    def test_tokyo_at_03_00_utc_yields_15_00_utc(self):
        # 03:00 UTC = 12:00 JST → next Tokyo midnight = 15:00 UTC same day.
        now = datetime(2026, 5, 3, 3, 0, 0, tzinfo=timezone.utc)
        midnight = next_midnight_in_tz(now, "Asia/Tokyo")
        assert midnight == datetime(2026, 5, 3, 15, 0, 0, tzinfo=timezone.utc)


class TestComputeSessionWindow:
    def test_returns_iso_pair_and_30d_ttl(self):
        now = datetime(2026, 5, 3, 16, 0, 0, tzinfo=timezone.utc)
        created, expires, ttl = compute_session_window("America/New_York", now)
        assert created == "2026-05-03T16:00:00Z"
        # Next NY midnight after 16:00 UTC (= 12:00 EDT) → next 04:00 UTC.
        assert expires == "2026-05-04T04:00:00Z"
        # 30d from now in epoch
        expected_ttl = int((now + timedelta(days=30)).timestamp())
        assert ttl == expected_ttl


class TestIsActive:
    def test_session_in_future_is_active(self):
        now = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)
        session = ReflectionSession(expires_at="2026-05-04T04:00:00Z")
        assert is_active(session, now) is True

    def test_session_in_past_is_not_active(self):
        now = datetime(2026, 5, 4, 5, 0, 0, tzinfo=timezone.utc)
        session = ReflectionSession(expires_at="2026-05-04T04:00:00Z")
        assert is_active(session, now) is False

    def test_empty_expires_at_is_not_active(self):
        session = ReflectionSession(expires_at="")
        assert is_active(session) is False

    def test_invalid_expires_at_is_not_active(self):
        session = ReflectionSession(expires_at="not-a-date")
        assert is_active(session) is False


class TestIsoRoundtrip:
    def test_parse_then_iso_lossless(self):
        original = "2026-05-03T16:00:00Z"
        assert iso(parse_iso(original)) == original
