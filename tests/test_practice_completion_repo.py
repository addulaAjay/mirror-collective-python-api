"""PracticeCompletionRepo unit tests (Phase 2.4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.app.models.practice_completion import PracticeCompletion
from src.app.repositories.practice_completion_repo import PracticeCompletionRepo
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

pytestmark = pytest.mark.asyncio


TABLE_NAME = "mc_practice_completions-test"


@pytest.fixture
def completion_table() -> FakeTable:
    return FakeTable(
        primary_key=["user_id", "completion_id"],
        indexes={"practice_id-completed_at-index": ["practice_id", "completed_at"]},
    )


@pytest.fixture
def repo(
    completion_table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> PracticeCompletionRepo:
    monkeypatch.setenv("DYNAMODB_PRACTICE_COMPLETIONS_TABLE", TABLE_NAME)
    return PracticeCompletionRepo(
        session=FakeAioSession({TABLE_NAME: completion_table})
    )


def _completion(
    completed_at: datetime,
    *,
    practice_id: str = "breath_4_6",
    helpful=None,
    user_id: str = "u1",
) -> PracticeCompletion:
    iso = completed_at.isoformat().replace("+00:00", "Z")
    return PracticeCompletion(
        user_id=user_id,
        session_id="s1",
        loop_id="pressure",
        tone_state="rising",
        practice_id=practice_id,
        rule_id="pressure_loop_v1",
        helpful=helpful,
        completed_at=iso,
    )


class TestPut:
    async def test_put_round_trips(self, repo: PracticeCompletionRepo):
        completion = _completion(datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc))
        await repo.put(completion)
        results = await repo.list_by_user_since(
            "u1", datetime(2026, 5, 3, tzinfo=timezone.utc)
        )
        assert len(results) == 1
        assert results[0].practice_id == "breath_4_6"
        assert results[0].user_hash != ""  # auto-derived in __post_init__

    async def test_completion_id_format(self, repo: PracticeCompletionRepo):
        completion = _completion(datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc))
        # completion_id is "<ts_iso>#<uuid>"
        assert "#" in completion.completion_id
        assert completion.completion_id.startswith(completion.completed_at)


class TestListByUserSince:
    async def test_returns_only_within_window(self, repo: PracticeCompletionRepo):
        now = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
        # Older than the window — should be excluded.
        await repo.put(_completion(now - timedelta(hours=20)))
        # Within window — should be included.
        await repo.put(_completion(now - timedelta(hours=2)))
        await repo.put(_completion(now - timedelta(hours=1)))

        cutoff = now - timedelta(hours=12)
        results = await repo.list_by_user_since("u1", cutoff)
        assert len(results) == 2

    async def test_does_not_return_other_users_completions(
        self, repo: PracticeCompletionRepo
    ):
        now = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
        await repo.put(_completion(now, user_id="u1", practice_id="breath_4_6"))
        await repo.put(_completion(now, user_id="u2", practice_id="breath_box_4"))
        cutoff = now - timedelta(days=1)
        u1 = await repo.list_by_user_since("u1", cutoff)
        u2 = await repo.list_by_user_since("u2", cutoff)
        assert {r.practice_id for r in u1} == {"breath_4_6"}
        assert {r.practice_id for r in u2} == {"breath_box_4"}


class TestUpdateHelpful:
    async def test_sets_helpful_on_existing_row(self, repo: PracticeCompletionRepo):
        completion = _completion(datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc))
        await repo.put(completion)
        updated = await repo.update_helpful(
            completion.user_id, completion.completion_id, helpful=True
        )
        assert updated is not None
        assert updated.helpful is True

    async def test_returns_none_for_unknown_completion(
        self, repo: PracticeCompletionRepo
    ):
        result = await repo.update_helpful("u1", "does-not-exist", helpful=False)
        assert result is None
