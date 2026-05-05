"""Unit tests for loop_state_updater (spec §8.3)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.app.models.echo_loop_state import EchoLoopState
from src.app.models.practice_completion import PracticeCompletion
from src.app.repositories.echo_loop_state_repo import EchoLoopStateRepo
from src.app.repositories.practice_completion_repo import PracticeCompletionRepo
from src.app.services.echo.loop_state_updater import apply_completion_delta
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

LOOPS = "mc_echo_loop_state-test"
COMPLETIONS = "mc_practice_completions-test"
NOW = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def repos(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DYNAMODB_ECHO_LOOP_STATE_TABLE", LOOPS)
    monkeypatch.setenv("DYNAMODB_PRACTICE_COMPLETIONS_TABLE", COMPLETIONS)
    sess = FakeAioSession(
        {
            LOOPS: FakeTable(primary_key=["user_id", "loop_id"]),
            COMPLETIONS: FakeTable(
                primary_key=["user_id", "completion_id"],
                indexes={
                    "practice_id-completed_at-index": ["practice_id", "completed_at"]
                },
            ),
        }
    )
    return {
        "loop_states": EchoLoopStateRepo(session=sess),
        "completions": PracticeCompletionRepo(session=sess),
    }


def _seed_loop(repos, **overrides) -> EchoLoopState:
    base = dict(
        user_id="u1",
        loop_id="pressure",
        tone_state="rising",
        intensity_score=0.74,
        intensity_label="High",
        last_seen=NOW.isoformat().replace("+00:00", "Z"),
    )
    base.update(overrides)
    state = EchoLoopState(**base)
    asyncio.run(repos["loop_states"].upsert(state))
    return state


def _put_completion(repos, **overrides) -> PracticeCompletion:
    base = dict(
        user_id="u1",
        session_id="s1",
        loop_id="pressure",
        tone_state="rising",
        practice_id="breath_4_6",
        rule_id="pressure_loop_v1",
        helpful=True,
        completed_at=NOW.isoformat().replace("+00:00", "Z"),
    )
    base.update(overrides)
    c = PracticeCompletion(**base)
    asyncio.run(repos["completions"].put(c))
    return c


# ============================================================
# helpful=True
# ============================================================


class TestHelpfulTrue:
    def test_reduces_intensity_by_0_10(self, repos):
        _seed_loop(repos, intensity_score=0.74)
        result = asyncio.run(
            apply_completion_delta(
                "u1",
                "pressure",
                helpful=True,
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                now=NOW,
            )
        )
        assert result is not None
        assert result.intensity_score == pytest.approx(0.64, abs=1e-3)

    def test_floors_at_zero(self, repos):
        _seed_loop(repos, intensity_score=0.05)
        result = asyncio.run(
            apply_completion_delta(
                "u1",
                "pressure",
                helpful=True,
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                now=NOW,
            )
        )
        assert result is not None
        assert result.intensity_score == 0.0

    def test_sets_softening_when_drop_meets_threshold(self, repos):
        _seed_loop(repos, intensity_score=0.74, tone_state="rising")
        result = asyncio.run(
            apply_completion_delta(
                "u1",
                "pressure",
                helpful=True,
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                now=NOW,
            )
        )
        # 0.10 drop ≥ 0.05 threshold → tone flips to softening.
        assert result.tone_state == "softening"
        assert result.recently_changed is True

    def test_does_not_flip_softening_when_floor_drop_below_threshold(self, repos):
        # Starting at 0.03 → drops 0.03 (clamped). 0.03 < 0.05 → no softening.
        _seed_loop(repos, intensity_score=0.03, tone_state="rising")
        result = asyncio.run(
            apply_completion_delta(
                "u1",
                "pressure",
                helpful=True,
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                now=NOW,
            )
        )
        assert result.intensity_score == 0.0
        assert result.tone_state == "rising"

    def test_intensity_label_recomputed(self, repos):
        _seed_loop(repos, intensity_score=0.74, intensity_label="High")
        result = asyncio.run(
            apply_completion_delta(
                "u1",
                "pressure",
                helpful=True,
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                now=NOW,
            )
        )
        # 0.64 → still "Medium" (≥0.33, <0.66)
        assert result.intensity_label == "Medium"

    def test_persists_to_repo(self, repos):
        _seed_loop(repos, intensity_score=0.74)
        asyncio.run(
            apply_completion_delta(
                "u1",
                "pressure",
                helpful=True,
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                now=NOW,
            )
        )
        loaded = asyncio.run(repos["loop_states"].get("u1", "pressure"))
        assert loaded.intensity_score == pytest.approx(0.64, abs=1e-3)
        assert loaded.tone_state == "softening"


# ============================================================
# helpful=False / None
# ============================================================


class TestNoMutationCases:
    def test_helpful_false_no_change(self, repos):
        _seed_loop(repos, intensity_score=0.74, tone_state="rising")
        result = asyncio.run(
            apply_completion_delta(
                "u1",
                "pressure",
                helpful=False,
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                now=NOW,
            )
        )
        assert result is None
        loaded = asyncio.run(repos["loop_states"].get("u1", "pressure"))
        assert loaded.intensity_score == 0.74
        assert loaded.tone_state == "rising"

    def test_helpful_none_no_change(self, repos):
        _seed_loop(repos, intensity_score=0.74)
        result = asyncio.run(
            apply_completion_delta(
                "u1",
                "pressure",
                helpful=None,
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                now=NOW,
            )
        )
        assert result is None
        loaded = asyncio.run(repos["loop_states"].get("u1", "pressure"))
        assert loaded.intensity_score == 0.74

    def test_no_loop_row_returns_none(self, repos):
        # No state exists for this user/loop yet.
        result = asyncio.run(
            apply_completion_delta(
                "u1",
                "pressure",
                helpful=True,
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                now=NOW,
            )
        )
        assert result is None
