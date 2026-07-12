"""Unit tests for LifeAnchorRepo (MirrorGPT Memory — Phase 2A)."""

from __future__ import annotations

import pytest

from src.app.models.life_anchor import AnchorScopes, LifeAnchor
from src.app.repositories.life_anchor_repo import LifeAnchorRepo
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

pytestmark = pytest.mark.asyncio

TABLE = "mc_life_anchors-test"


@pytest.fixture
def table() -> FakeTable:
    return FakeTable(
        primary_key=["user_id", "anchor_id"],
        indexes={"status-index": ["user_id", "status"]},
    )


@pytest.fixture
def repo(table: FakeTable, monkeypatch: pytest.MonkeyPatch) -> LifeAnchorRepo:
    monkeypatch.setenv("DYNAMODB_LIFE_ANCHORS_TABLE", TABLE)
    return LifeAnchorRepo(session=FakeAioSession({TABLE: table}))


def _anchor(user_id: str = "u1", anchor_id: str = "a1", **kw) -> LifeAnchor:
    return LifeAnchor(
        user_id=user_id, anchor_id=anchor_id, title="Wife passed away", **kw
    )


class TestUpsertAndGet:
    async def test_roundtrip_preserves_all_fields(self, repo: LifeAnchorRepo):
        a = _anchor(
            anchor_type="loss",
            emotional_weight="sacred",
            relationship="wife",
            reflection_use="always_consider",
            scopes=AnchorScopes(mirrorgpt=True, echo_vault=True),
            tone_guidance=["Do not say time heals everything."],
        )
        await repo.upsert(a)

        got = await repo.get("u1", "a1")
        assert got is not None
        assert got.anchor_type == "loss"
        assert got.emotional_weight == "sacred"
        assert got.relationship == "wife"
        assert got.reflection_use == "always_consider"
        assert got.scopes.mirrorgpt is True
        assert got.scopes.echo_vault is True
        assert got.scopes.echo_map is False
        assert got.tone_guidance == ["Do not say time heals everything."]

    async def test_get_missing_returns_none(self, repo: LifeAnchorRepo):
        assert await repo.get("u1", "does-not-exist") is None


class TestQueryByUser:
    async def test_returns_only_that_users_rows(self, repo: LifeAnchorRepo):
        await repo.upsert(_anchor("u1", "a1"))
        await repo.upsert(_anchor("u1", "a2"))
        await repo.upsert(_anchor("u2", "b1"))

        rows = await repo.query_by_user("u1")
        assert {r.anchor_id for r in rows} == {"a1", "a2"}


class TestListActiveForUser:
    async def test_filters_paused_and_non_mirrorgpt(self, repo: LifeAnchorRepo):
        await repo.upsert(
            _anchor(
                "u1", "active", status="active", scopes=AnchorScopes(mirrorgpt=True)
            )
        )
        await repo.upsert(
            _anchor(
                "u1", "paused", status="paused", scopes=AnchorScopes(mirrorgpt=True)
            )
        )
        await repo.upsert(
            _anchor(
                "u1", "noscope", status="active", scopes=AnchorScopes(mirrorgpt=False)
            )
        )

        active = await repo.list_active_for_user("u1")
        assert {a.anchor_id for a in active} == {"active"}


class TestDelete:
    async def test_delete_removes_row(self, repo: LifeAnchorRepo):
        await repo.upsert(_anchor("u1", "a1"))
        await repo.delete("u1", "a1")
        assert await repo.get("u1", "a1") is None

    async def test_delete_is_scoped_to_user(self, repo: LifeAnchorRepo):
        await repo.upsert(_anchor("u1", "a1"))
        await repo.upsert(_anchor("u2", "a1"))
        # Deleting u1's row must not touch u2's same-SK row.
        await repo.delete("u1", "a1")
        assert await repo.get("u1", "a1") is None
        assert await repo.get("u2", "a1") is not None
