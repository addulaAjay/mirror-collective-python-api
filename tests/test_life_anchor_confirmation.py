"""Unit tests for the in-chat Life Anchor confirm flow (Phase 2D)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.app.repositories.life_anchor_repo import LifeAnchorRepo
from src.app.services import life_anchor_confirmation as lac
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

TABLE = "mc_life_anchors-test"


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch) -> LifeAnchorRepo:
    monkeypatch.setenv("DYNAMODB_LIFE_ANCHORS_TABLE", TABLE)
    sess = FakeAioSession(
        {
            TABLE: FakeTable(
                primary_key=["user_id", "anchor_id"],
                indexes={"status-index": ["user_id", "status"]},
            )
        }
    )
    return LifeAnchorRepo(session=sess)


def _candidate(**kw) -> dict:
    base = {
        "prompt": "Would you like me to remember this?",
        "candidate_text": "My wife passed away last year",
        "anchor_type_guess": "loss",
        "emotional_weight_guess": "sacred",
    }
    base.update(kw)
    return base


def _stub_structurer(result=None) -> MagicMock:
    s = MagicMock()
    s.structure = AsyncMock(return_value=result)
    return s


class TestClassifyReply:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("yes please", "affirmative"),
            ("yeah, remember that", "affirmative"),
            ("sure, hold it", "affirmative"),
            ("no thanks", "negative"),
            ("not now", "negative"),
            ("never", "negative"),
            ("I've been thinking about my career lately", "other"),
            ("", "other"),
            ("yes but actually no", "other"),  # ambiguous → other
        ],
    )
    def test_classify(self, message, expected):
        assert lac.classify_reply(message) == expected


class TestStorePending:
    @pytest.mark.asyncio
    async def test_creates_pending_row(self, repo: LifeAnchorRepo):
        await lac.store_pending(repo, "u1", "c1", _candidate())
        got = await repo.get("u1", lac.get_pending_anchor_id("c1"))
        assert got is not None
        assert got.status == "pending"
        assert got.user_confirmed is False
        assert got.anchor_type == "loss"
        assert got.emotional_weight == "sacred"
        # A pending row is never surfaced to chat.
        assert await repo.list_active_for_user("u1") == []


class TestResolvePending:
    @pytest.mark.asyncio
    async def test_no_pending_returns_false(self, repo: LifeAnchorRepo):
        out = await lac.resolve_pending(
            repo, _stub_structurer(), "u1", "c1", "affirmative"
        )
        assert out is False

    @pytest.mark.asyncio
    async def test_affirmative_creates_active_and_clears_pending(
        self, repo: LifeAnchorRepo
    ):
        await lac.store_pending(repo, "u1", "c1", _candidate())

        out = await lac.resolve_pending(
            repo, _stub_structurer(), "u1", "c1", "affirmative"
        )
        await asyncio.sleep(0)  # let the fire-and-forget enrichment settle

        assert out is True
        assert await repo.get("u1", lac.get_pending_anchor_id("c1")) is None
        actives = await repo.list_active_for_user("u1")
        assert len(actives) == 1
        a = actives[0]
        assert a.status == "active"
        assert a.user_confirmed is True
        assert a.anchor_type == "loss"
        assert a.reflection_use == "always_consider"  # sacred → always
        assert not a.anchor_id.startswith("pending#")  # fresh uuid

    @pytest.mark.asyncio
    async def test_negative_discards_pending_without_creating(
        self, repo: LifeAnchorRepo
    ):
        await lac.store_pending(repo, "u1", "c1", _candidate())

        out = await lac.resolve_pending(
            repo, _stub_structurer(), "u1", "c1", "negative"
        )

        assert out is False
        assert await repo.get("u1", lac.get_pending_anchor_id("c1")) is None
        assert await repo.list_active_for_user("u1") == []

    @pytest.mark.asyncio
    async def test_enrichment_upgrades_anchor(self, repo: LifeAnchorRepo):
        await lac.store_pending(repo, "u1", "c1", _candidate())
        structurer = _stub_structurer(
            {
                "anchor_type": "loss",
                "title": "User's wife passed away",
                "relationship": "wife",
                "emotional_weight": "sacred",
                "tone_guidance": ["Do not say time heals everything."],
            }
        )

        await lac.resolve_pending(repo, structurer, "u1", "c1", "affirmative")
        await asyncio.sleep(0)  # allow enrichment task to run

        actives = await repo.list_active_for_user("u1")
        assert len(actives) == 1
        assert actives[0].title == "User's wife passed away"
        assert actives[0].relationship == "wife"
        assert actives[0].tone_guidance == ["Do not say time heals everything."]
