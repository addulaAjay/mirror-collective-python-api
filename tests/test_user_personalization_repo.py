"""UserPersonalizationRepo unit tests (Phase 2.5)."""

from __future__ import annotations

import pytest

from src.app.models.user_personalization import UserPersonalization
from src.app.repositories.user_personalization_repo import UserPersonalizationRepo
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

pytestmark = pytest.mark.asyncio


TABLE_NAME = "mc_user_personalization-test"


@pytest.fixture
def prefs_table() -> FakeTable:
    return FakeTable(primary_key=["user_id"])


@pytest.fixture
def repo(
    prefs_table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> UserPersonalizationRepo:
    monkeypatch.setenv("DYNAMODB_USER_PERSONALIZATION_TABLE", TABLE_NAME)
    return UserPersonalizationRepo(session=FakeAioSession({TABLE_NAME: prefs_table}))


class TestGetOrDefault:
    async def test_returns_default_for_new_user(self, repo: UserPersonalizationRepo):
        prefs = await repo.get_or_default("brand-new")
        assert isinstance(prefs, UserPersonalization)
        assert prefs.user_id == "brand-new"
        assert prefs.flags.no_breathwork is False
        assert prefs.flags.reduced_motion is False
        assert prefs.flags.private_mode is False
        assert prefs.disallow_types == []
        assert prefs.practice_helpfulness == {}

    async def test_returns_stored_prefs_when_present(
        self, repo: UserPersonalizationRepo
    ):
        await repo.set_flags("u1", no_breathwork=True)
        prefs = await repo.get_or_default("u1")
        assert prefs.flags.no_breathwork is True


class TestSetFlags:
    async def test_partial_update_only_changes_specified_flags(
        self, repo: UserPersonalizationRepo
    ):
        await repo.set_flags("u1", no_breathwork=True)
        await repo.set_flags("u1", reduced_motion=True)
        prefs = await repo.get_or_default("u1")
        assert prefs.flags.no_breathwork is True
        assert prefs.flags.reduced_motion is True
        assert prefs.flags.private_mode is False


class TestRecordCompletion:
    async def test_increments_recent_use_and_bucket(
        self, repo: UserPersonalizationRepo
    ):
        prefs = await repo.record_completion(
            "u1", practice_id="breath_4_6", time_of_day_bucket="morning"
        )
        assert "breath_4_6" in prefs.recent_use
        assert prefs.recent_use["breath_4_6"].count_30d == 1
        assert prefs.time_of_day_history.get("morning") == 1

    async def test_subsequent_completion_increments_count(
        self, repo: UserPersonalizationRepo
    ):
        await repo.record_completion("u1", "breath_4_6", "morning")
        await repo.record_completion("u1", "breath_4_6", "morning")
        prefs = await repo.get_or_default("u1")
        assert prefs.recent_use["breath_4_6"].count_30d == 2
        assert prefs.time_of_day_history["morning"] == 2


class TestRecordHelpfulness:
    async def test_appends_event_per_practice(self, repo: UserPersonalizationRepo):
        prefs = await repo.record_helpfulness(
            "u1", "breath_4_6", helpful=True, ts="2026-05-03T12:00:00Z"
        )
        assert len(prefs.practice_helpfulness["breath_4_6"]) == 1
        assert prefs.practice_helpfulness["breath_4_6"][0].helpful is True

        prefs = await repo.record_helpfulness(
            "u1", "breath_4_6", helpful=False, ts="2026-05-03T13:00:00Z"
        )
        assert len(prefs.practice_helpfulness["breath_4_6"]) == 2
        # Most recent event preserved
        assert prefs.practice_helpfulness["breath_4_6"][-1].helpful is False

    async def test_separate_practices_tracked_independently(
        self, repo: UserPersonalizationRepo
    ):
        await repo.record_helpfulness("u1", "breath_4_6", helpful=True)
        await repo.record_helpfulness("u1", "breath_box_4", helpful=False)
        prefs = await repo.get_or_default("u1")
        assert "breath_4_6" in prefs.practice_helpfulness
        assert "breath_box_4" in prefs.practice_helpfulness


class TestUpsertPersistence:
    async def test_full_round_trip_via_upsert(self, repo: UserPersonalizationRepo):
        prefs = UserPersonalization(user_id="u1")
        prefs.flags.no_breathwork = True
        prefs.disallow_types = ["somatic"]
        prefs.append_helpfulness("breath_4_6", True, "2026-05-03T12:00:00Z")
        prefs.record_use("breath_4_6")
        prefs.increment_bucket("morning")

        await repo.upsert(prefs)
        loaded = await repo.get_or_default("u1")
        assert loaded.flags.no_breathwork is True
        assert loaded.disallow_types == ["somatic"]
        assert len(loaded.practice_helpfulness["breath_4_6"]) == 1
        assert loaded.recent_use["breath_4_6"].count_30d == 1
        assert loaded.time_of_day_history["morning"] == 1
