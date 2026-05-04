"""EchoLoopStateRepo unit tests (Phase 2.3)."""

from __future__ import annotations

import pytest

from src.app.models.echo_loop_state import EchoLoopState
from src.app.repositories.echo_loop_state_repo import EchoLoopStateRepo
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

pytestmark = pytest.mark.asyncio


TABLE_NAME = "mc_echo_loop_state-test"


@pytest.fixture
def loop_state_table() -> FakeTable:
    # PK=user_id, SK=loop_id
    return FakeTable(primary_key=["user_id", "loop_id"])


@pytest.fixture
def repo(
    loop_state_table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> EchoLoopStateRepo:
    monkeypatch.setenv("DYNAMODB_ECHO_LOOP_STATE_TABLE", TABLE_NAME)
    return EchoLoopStateRepo(session=FakeAioSession({TABLE_NAME: loop_state_table}))


def _state(loop_id: str, **overrides) -> EchoLoopState:
    base = dict(
        user_id="u1",
        loop_id=loop_id,
        tone_state="rising",
        intensity_score=0.74,
        intensity_label="High",
    )
    base.update(overrides)
    return EchoLoopState(**base)


class TestUpsertAndQuery:
    async def test_upsert_then_query_by_user_returns_row(self, repo: EchoLoopStateRepo):
        await repo.upsert(_state("pressure"))
        rows = await repo.query_by_user("u1")
        assert len(rows) == 1
        assert rows[0].loop_id == "pressure"
        assert rows[0].intensity_score == 0.74  # float round-trip OK

    async def test_query_by_user_returns_all_loops_for_user(
        self, repo: EchoLoopStateRepo
    ):
        await repo.upsert(_state("pressure"))
        await repo.upsert(_state("grief", tone_state="softening", intensity_score=0.58))
        await repo.upsert(_state("agency", tone_state="steady", intensity_score=0.61))
        rows = await repo.query_by_user("u1")
        assert {r.loop_id for r in rows} == {"pressure", "grief", "agency"}

    async def test_query_by_user_does_not_leak_across_users(
        self, repo: EchoLoopStateRepo
    ):
        await repo.upsert(_state("pressure"))
        await repo.upsert(_state("grief", user_id="u2"))
        rows_u1 = await repo.query_by_user("u1")
        rows_u2 = await repo.query_by_user("u2")
        assert {r.loop_id for r in rows_u1} == {"pressure"}
        assert {r.loop_id for r in rows_u2} == {"grief"}

    async def test_upsert_overwrites_existing_row(self, repo: EchoLoopStateRepo):
        # First write
        await repo.upsert(_state("pressure", intensity_score=0.74, tone_state="rising"))
        # Second write with same key, different fields
        await repo.upsert(
            _state("pressure", intensity_score=0.50, tone_state="softening")
        )
        rows = await repo.query_by_user("u1")
        assert len(rows) == 1
        assert rows[0].intensity_score == 0.50
        assert rows[0].tone_state == "softening"


class TestUpsertMany:
    async def test_writes_all_states(self, repo: EchoLoopStateRepo):
        states = [
            _state("pressure", intensity_score=0.74),
            _state("grief", tone_state="softening", intensity_score=0.58),
            _state("agency", tone_state="rising", intensity_score=0.65),
        ]
        results = await repo.upsert_many(states)
        assert len(results) == 3
        rows = await repo.query_by_user("u1")
        assert {r.loop_id for r in rows} == {"pressure", "grief", "agency"}


class TestGetSingle:
    async def test_get_returns_row_when_present(self, repo: EchoLoopStateRepo):
        await repo.upsert(_state("pressure"))
        row = await repo.get("u1", "pressure")
        assert row is not None
        assert row.loop_id == "pressure"

    async def test_get_returns_none_when_missing(self, repo: EchoLoopStateRepo):
        assert await repo.get("u1", "pressure") is None


class TestDeleteForUser:
    async def test_removes_all_rows_for_user(self, repo: EchoLoopStateRepo):
        await repo.upsert(_state("pressure"))
        await repo.upsert(_state("grief"))
        deleted = await repo.delete_for_user("u1")
        assert deleted == 2
        assert await repo.query_by_user("u1") == []

    async def test_returns_0_when_no_rows(self, repo: EchoLoopStateRepo):
        assert await repo.delete_for_user("u1") == 0
