"""Unit tests for cooldown_enforcer (spec §B.2.7 + §9.4)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.app.models.practice_completion import PracticeCompletion
from src.app.repositories.practice_completion_repo import PracticeCompletionRepo
from src.app.services.practice.catalog_loader import load_practice_catalog
from src.app.services.practice.cooldown_enforcer import apply
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

TABLE = "mc_practice_completions-test"
NOW = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DYNAMODB_PRACTICE_COMPLETIONS_TABLE", TABLE)
    table = FakeTable(
        primary_key=["user_id", "completion_id"],
        indexes={"practice_id-completed_at-index": ["practice_id", "completed_at"]},
    )
    return PracticeCompletionRepo(session=FakeAioSession({TABLE: table}))


@pytest.fixture
def catalog():
    return load_practice_catalog()


def _completion(practice_id: str, hours_ago: int) -> PracticeCompletion:
    ts = (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")
    return PracticeCompletion(
        user_id="u1",
        session_id="s1",
        loop_id="pressure",
        tone_state="rising",
        practice_id=practice_id,
        rule_id="pressure_loop_v1",
        completed_at=ts,
    )


def test_no_recent_completions_keeps_all(repo, catalog):
    candidates = [catalog.get("breath_4_6"), catalog.get("name_and_need")]
    out = asyncio.run(apply(candidates, "u1", 12, completions_repo=repo, now=NOW))
    assert {p.id for p in out} == {"breath_4_6", "name_and_need"}


def test_within_cooldown_dropped(repo, catalog):
    asyncio.run(repo.put(_completion("breath_4_6", hours_ago=6)))
    candidates = [catalog.get("breath_4_6"), catalog.get("name_and_need")]
    out = asyncio.run(apply(candidates, "u1", 12, completions_repo=repo, now=NOW))
    assert {p.id for p in out} == {"name_and_need"}


def test_outside_cooldown_kept(repo, catalog):
    asyncio.run(repo.put(_completion("breath_4_6", hours_ago=13)))
    candidates = [catalog.get("breath_4_6")]
    out = asyncio.run(apply(candidates, "u1", 12, completions_repo=repo, now=NOW))
    assert {p.id for p in out} == {"breath_4_6"}


def test_grief_24h_cooldown(repo, catalog):
    # Completed 20h ago — still within 24h grief cooldown.
    asyncio.run(repo.put(_completion("heart_hand_breath", hours_ago=20)))
    candidates = [catalog.get("heart_hand_breath")]
    out = asyncio.run(apply(candidates, "u1", 24, completions_repo=repo, now=NOW))
    assert out == []


def test_other_users_completions_ignored(repo, catalog):
    other_completion = PracticeCompletion(
        user_id="other-user",
        session_id="s1",
        loop_id="pressure",
        tone_state="rising",
        practice_id="breath_4_6",
        rule_id="pressure_loop_v1",
        completed_at=(NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
    )
    asyncio.run(repo.put(other_completion))
    candidates = [catalog.get("breath_4_6")]
    out = asyncio.run(apply(candidates, "u1", 12, completions_repo=repo, now=NOW))
    assert {p.id for p in out} == {"breath_4_6"}
