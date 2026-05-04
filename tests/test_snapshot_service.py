"""Unit tests for snapshot_service (spec §8.1 + §B.3.2)."""

from __future__ import annotations

import asyncio
from typing import Dict

import pytest

from src.app.core.exceptions import NotFoundError
from src.app.models.echo_loop_state import EchoLoopState
from src.app.models.reflection_session import ReflectionSession
from src.app.repositories.echo_loop_state_repo import EchoLoopStateRepo
from src.app.repositories.reflection_session_repo import (
    GSI_USER_CREATED,
    ReflectionSessionRepo,
)
from src.app.services.echo.snapshot_service import build_snapshot
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

SESSIONS_TABLE = "mc_reflection_sessions-test"
LOOP_STATE_TABLE = "mc_echo_loop_state-test"


@pytest.fixture
def tables() -> Dict[str, FakeTable]:
    return {
        SESSIONS_TABLE: FakeTable(
            primary_key=["session_id"],
            indexes={GSI_USER_CREATED: ["user_id", "created_at"]},
        ),
        LOOP_STATE_TABLE: FakeTable(primary_key=["user_id", "loop_id"]),
    }


@pytest.fixture
def repos(tables: Dict[str, FakeTable], monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DYNAMODB_REFLECTION_SESSIONS_TABLE", SESSIONS_TABLE)
    monkeypatch.setenv("DYNAMODB_ECHO_LOOP_STATE_TABLE", LOOP_STATE_TABLE)
    sess = FakeAioSession(tables)
    return {
        "sessions": ReflectionSessionRepo(session=sess),
        "loop_states": EchoLoopStateRepo(session=sess),
    }


def _put_session(repos, **overrides) -> ReflectionSession:
    base = dict(
        user_id="u1",
        motif_id="spiral",
        motif_name="Spiral",
        room_skin="Spiral Room",
        motif_payload={"override_allowed": False},
        quiz_answers={
            "q1": "hopeful",
            "q2": "inspiration",
            "q3": "spiral",
            "q4": "insight",
        },
        scores={"evolution": 5},
        user_tz="America/New_York",
        expires_at="2099-01-01T00:00:00Z",
        created_at="2026-05-03T10:00:00Z",
    )
    base.update(overrides)
    session = ReflectionSession(**base)
    asyncio.run(repos["sessions"].put(session))
    return session


def _put_loop(repos, user_id="u1", **overrides) -> EchoLoopState:
    base = dict(
        user_id=user_id,
        loop_id="pressure",
        tone_state="rising",
        intensity_score=0.74,
        intensity_label="High",
        last_seen="2026-05-03T20:10:00Z",
        recently_changed=False,
    )
    base.update(overrides)
    state = EchoLoopState(**base)
    asyncio.run(repos["loop_states"].upsert(state))
    return state


# ============================================================
# Empty state
# ============================================================


def test_empty_user_returns_empty_loops(repos):
    session = _put_session(repos)
    snapshot = asyncio.run(
        build_snapshot(
            user_id="u1",
            session_id=session.session_id,
            sessions_repo=repos["sessions"],
            loop_state_repo=repos["loop_states"],
        )
    )
    assert snapshot.loops == []
    assert snapshot.session_id == session.session_id
    assert snapshot.motif_id == "spiral"
    assert snapshot.room_skin == "Spiral Room"
    assert snapshot.updated_at  # populated


# ============================================================
# Sorted by intensity desc
# ============================================================


def test_loops_sorted_by_intensity_desc(repos):
    session = _put_session(repos)
    _put_loop(repos, loop_id="pressure", intensity_score=0.74)
    _put_loop(repos, loop_id="grief", intensity_score=0.58, tone_state="softening")
    _put_loop(repos, loop_id="agency", intensity_score=0.65, tone_state="rising")

    snapshot = asyncio.run(
        build_snapshot(
            user_id="u1",
            session_id=session.session_id,
            sessions_repo=repos["sessions"],
            loop_state_repo=repos["loop_states"],
        )
    )
    assert [l.loop_id for l in snapshot.loops] == ["pressure", "agency", "grief"]
    assert [l.intensity_score for l in snapshot.loops] == [0.74, 0.65, 0.58]


# ============================================================
# Tone library enrichment (icon + reflection_line populated)
# ============================================================


def test_loops_enriched_with_icon_and_reflection_line(repos):
    session = _put_session(repos)
    _put_loop(repos, loop_id="pressure", tone_state="rising")
    snapshot = asyncio.run(
        build_snapshot(
            user_id="u1",
            session_id=session.session_id,
            sessions_repo=repos["sessions"],
            loop_state_repo=repos["loop_states"],
        )
    )
    assert len(snapshot.loops) == 1
    loop = snapshot.loops[0]
    assert loop.icon == "🔺"
    assert "Pressure is climbing" in loop.reflection_line


# ============================================================
# Filtering: unsupported loops + zero-intensity rows
# ============================================================


def test_unsupported_loop_id_filtered_out(repos):
    session = _put_session(repos)
    _put_loop(repos, loop_id="pressure")
    # Insert a forward-compat row directly via the repo to bypass any model-side guards.
    asyncio.run(
        repos["loop_states"].upsert(
            EchoLoopState(
                user_id="u1",
                loop_id="clarity",  # not in V1 supported set
                tone_state="rising",
                intensity_score=0.7,
                intensity_label="High",
                last_seen="2026-05-03T20:10:00Z",
            )
        )
    )
    snapshot = asyncio.run(
        build_snapshot(
            user_id="u1",
            session_id=session.session_id,
            sessions_repo=repos["sessions"],
            loop_state_repo=repos["loop_states"],
        )
    )
    assert [l.loop_id for l in snapshot.loops] == ["pressure"]


def test_zero_intensity_loop_excluded(repos):
    session = _put_session(repos)
    _put_loop(repos, loop_id="pressure", intensity_score=0.0)
    snapshot = asyncio.run(
        build_snapshot(
            user_id="u1",
            session_id=session.session_id,
            sessions_repo=repos["sessions"],
            loop_state_repo=repos["loop_states"],
        )
    )
    assert snapshot.loops == []


# ============================================================
# Session resolution
# ============================================================


def test_session_id_omitted_uses_latest_session(repos):
    older = _put_session(
        repos,
        session_id="s_old",
        created_at="2026-05-01T10:00:00Z",
    )
    newer = _put_session(
        repos,
        session_id="s_new",
        created_at="2026-05-03T10:00:00Z",
        motif_id="mirror",
        room_skin="Mirror Room",
    )
    snapshot = asyncio.run(
        build_snapshot(
            user_id="u1",
            session_id=None,
            sessions_repo=repos["sessions"],
            loop_state_repo=repos["loop_states"],
        )
    )
    assert snapshot.session_id == "s_new"
    assert snapshot.motif_id == "mirror"


def test_invalid_session_id_raises_not_found(repos):
    with pytest.raises(NotFoundError):
        asyncio.run(
            build_snapshot(
                user_id="u1",
                session_id="does-not-exist",
                sessions_repo=repos["sessions"],
                loop_state_repo=repos["loop_states"],
            )
        )


def test_session_for_other_user_treated_as_not_found(repos):
    other = _put_session(repos, user_id="other-user", session_id="s_other")
    with pytest.raises(NotFoundError):
        asyncio.run(
            build_snapshot(
                user_id="u1",
                session_id=other.session_id,
                sessions_repo=repos["sessions"],
                loop_state_repo=repos["loop_states"],
            )
        )


def test_no_session_at_all_raises_not_found(repos):
    with pytest.raises(NotFoundError):
        asyncio.run(
            build_snapshot(
                user_id="u1",
                session_id=None,
                sessions_repo=repos["sessions"],
                loop_state_repo=repos["loop_states"],
            )
        )


# ============================================================
# Effective room skin honors override
# ============================================================


def test_room_skin_override_wins_over_default(repos):
    session = _put_session(
        repos, room_skin="Spiral Room", room_skin_override="Mirror Room"
    )
    snapshot = asyncio.run(
        build_snapshot(
            user_id="u1",
            session_id=session.session_id,
            sessions_repo=repos["sessions"],
            loop_state_repo=repos["loop_states"],
        )
    )
    assert snapshot.room_skin == "Mirror Room"
