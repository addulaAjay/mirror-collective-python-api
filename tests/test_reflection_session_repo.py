"""ReflectionSessionRepo unit tests (Phase 2.2).

Drives the repo against the in-memory FakeDynamoDB shim — same boto3
condition syntax as production, but no DynamoDB Local needed.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from src.app.models.reflection_session import ReflectionSession
from src.app.repositories.reflection_session_repo import (
    GSI_USER_CREATED,
    ReflectionSessionRepo,
)
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

pytestmark = pytest.mark.asyncio


TABLE_NAME = "mc_reflection_sessions-test"


@pytest.fixture
def session_table() -> FakeTable:
    return FakeTable(
        primary_key=["session_id"],
        indexes={GSI_USER_CREATED: ["user_id", "created_at"]},
    )


@pytest.fixture
def repo(
    session_table: FakeTable, monkeypatch: pytest.MonkeyPatch
) -> ReflectionSessionRepo:
    monkeypatch.setenv("DYNAMODB_REFLECTION_SESSIONS_TABLE", TABLE_NAME)
    return ReflectionSessionRepo(session=FakeAioSession({TABLE_NAME: session_table}))


def _make_session(**overrides) -> ReflectionSession:
    base = dict(
        user_id="u1",
        motif_id="spiral",
        motif_name="Spiral",
        room_skin="Spiral Room",
        motif_payload={"tone_tag": "Evolution / Integration"},
        quiz_answers={
            "q1": "hopeful",
            "q2": "inspiration",
            "q3": "spiral",
            "q4": "insight",
        },
        scores={"evolution": 6, "illumination": 4, "clarity": 3},
        user_tz="America/New_York",
        expires_at="2026-05-04T04:00:00Z",
    )
    base.update(overrides)
    return ReflectionSession(**base)


class TestPutAndGet:
    async def test_put_returns_same_session(self, repo: ReflectionSessionRepo):
        session = _make_session()
        result = await repo.put(session)
        assert result is session

    async def test_get_after_put_round_trips(self, repo: ReflectionSessionRepo):
        session = _make_session(motif_id="mirror", motif_name="Mirror")
        await repo.put(session)
        loaded = await repo.get(session.session_id)
        assert loaded is not None
        assert loaded.motif_id == "mirror"
        assert loaded.user_id == "u1"
        assert loaded.scores == {"evolution": 6, "illumination": 4, "clarity": 3}

    async def test_get_missing_returns_none(self, repo: ReflectionSessionRepo):
        assert await repo.get("does-not-exist") is None


class TestGetLatestForUser:
    async def test_returns_most_recent_by_created_at(self, repo: ReflectionSessionRepo):
        # Created earlier (older).
        old = _make_session(
            session_id="s_old",
            created_at="2026-05-01T10:00:00Z",
        )
        # Created later (newer).
        new = _make_session(
            session_id="s_new",
            created_at="2026-05-03T10:00:00Z",
        )
        await repo.put(old)
        await repo.put(new)

        latest = await repo.get_latest_for_user("u1")
        assert latest is not None
        assert latest.session_id == "s_new"

    async def test_returns_none_when_no_sessions(self, repo: ReflectionSessionRepo):
        assert await repo.get_latest_for_user("nobody") is None


class TestUpdateRoomSkin:
    async def test_sets_override_on_existing_session(self, repo: ReflectionSessionRepo):
        session = _make_session()
        await repo.put(session)

        updated = await repo.update_room_skin(session.session_id, "Mirror Room")
        assert updated is not None
        assert updated.room_skin_override == "Mirror Room"
        assert updated.effective_room_skin() == "Mirror Room"
        # Default room_skin is preserved unchanged.
        assert updated.room_skin == "Spiral Room"

    async def test_returns_none_for_unknown_session(self, repo: ReflectionSessionRepo):
        result = await repo.update_room_skin("does-not-exist", "Anything")
        assert result is None


class TestUpdateMotifAndQuiz:
    async def test_overwrites_motif_and_clears_override(
        self, repo: ReflectionSessionRepo
    ):
        session = _make_session(room_skin_override="Mirror Room")
        await repo.put(session)

        updated = await repo.update_motif_and_quiz(
            session_id=session.session_id,
            motif_id="compass",
            motif_name="Compass",
            room_skin="Compass Room",
            motif_payload={"tone_tag": "Direction / Choice"},
            quiz_answers={
                "q1": "scattered",
                "q2": "clarity",
                "q3": "compass",
                "q4": "direct",
            },
            scores={"direction": 4, "clarity": 3},
        )
        assert updated is not None
        assert updated.motif_id == "compass"
        assert updated.motif_name == "Compass"
        assert updated.room_skin == "Compass Room"
        assert updated.room_skin_override is None  # cleared on retake
        assert updated.scores == {"direction": 4, "clarity": 3}
