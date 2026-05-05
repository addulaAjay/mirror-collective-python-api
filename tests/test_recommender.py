"""Unit tests for recommender (spec §B.2.9 + §9 + §6.3)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict

import pytest

from src.app.core.exceptions import (
    AllCandidatesFiltered,
    FallbackOnCooldown,
    LoopNotSupported,
    NoActiveLoops,
    NoRuleMatched,
)
from src.app.models.echo_loop_state import EchoLoopState
from src.app.models.practice_completion import PracticeCompletion
from src.app.models.reflection_session import ReflectionSession
from src.app.models.user_personalization import UserFlags, UserPersonalization
from src.app.repositories.echo_loop_state_repo import EchoLoopStateRepo
from src.app.repositories.practice_completion_repo import PracticeCompletionRepo
from src.app.repositories.reflection_session_repo import (
    GSI_USER_CREATED,
    ReflectionSessionRepo,
)
from src.app.repositories.user_personalization_repo import UserPersonalizationRepo
from src.app.services.practice.recommender import recommend
from src.app.services.practice.settings_loader import (
    MicroPracticeDefaults,
    MicroPracticeSettings,
)
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

SESSIONS = "mc_reflection_sessions-test"
LOOPS = "mc_echo_loop_state-test"
COMPLETIONS = "mc_practice_completions-test"
PREFS = "mc_user_personalization-test"
NOW = datetime(2026, 5, 3, 14, 0, tzinfo=timezone.utc)


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def repos(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DYNAMODB_REFLECTION_SESSIONS_TABLE", SESSIONS)
    monkeypatch.setenv("DYNAMODB_ECHO_LOOP_STATE_TABLE", LOOPS)
    monkeypatch.setenv("DYNAMODB_PRACTICE_COMPLETIONS_TABLE", COMPLETIONS)
    monkeypatch.setenv("DYNAMODB_USER_PERSONALIZATION_TABLE", PREFS)
    tables = {
        SESSIONS: FakeTable(
            primary_key=["session_id"],
            indexes={GSI_USER_CREATED: ["user_id", "created_at"]},
        ),
        LOOPS: FakeTable(primary_key=["user_id", "loop_id"]),
        COMPLETIONS: FakeTable(
            primary_key=["user_id", "completion_id"],
            indexes={"practice_id-completed_at-index": ["practice_id", "completed_at"]},
        ),
        PREFS: FakeTable(primary_key=["user_id"]),
    }
    sess = FakeAioSession(tables)
    return {
        "sessions": ReflectionSessionRepo(session=sess),
        "loop_states": EchoLoopStateRepo(session=sess),
        "completions": PracticeCompletionRepo(session=sess),
        "prefs": UserPersonalizationRepo(session=sess),
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
    s = ReflectionSession(**base)
    asyncio.run(repos["sessions"].put(s))
    return s


def _put_loop(repos, user_id="u1", **overrides) -> EchoLoopState:
    base = dict(
        user_id=user_id,
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


def _settings(fallback_enabled: bool = True) -> MicroPracticeSettings:
    return MicroPracticeSettings(
        version=1,
        defaults=MicroPracticeDefaults(
            cooldown_hours_default=12,
            cooldown_hours_grief=24,
            fallback_enabled=fallback_enabled,
        ),
    )


# ============================================================
# Spec §B.2.9 table
# ============================================================


class TestRuleMatchHappyPath:
    def test_pressure_high_rising_returns_pressure_practice(self, repos):
        _put_session(repos)
        _put_loop(repos, loop_id="pressure", intensity_score=0.74)

        result = asyncio.run(
            recommend(
                user_id="u1",
                session_id=None,
                selected_loop=None,
                surface="echo_signature",
                sessions_repo=repos["sessions"],
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                prefs_repo=repos["prefs"],
                settings=_settings(),
                now=NOW,
            )
        )
        assert result.rule_id == "pressure_loop_v1"
        assert result.practice.id in {
            "breath_4_6",
            "reappraisal_alt_intent",
            "one_percent_first_sentence",
        }
        assert result.pattern.loop_id == "pressure"


class TestNoActiveLoops:
    def test_all_below_threshold_raises(self, repos):
        _put_session(repos)
        _put_loop(
            repos,
            loop_id="pressure",
            intensity_score=0.3,
            tone_state="rising",
            recently_changed=False,
        )
        with pytest.raises(NoActiveLoops):
            asyncio.run(
                recommend(
                    user_id="u1",
                    session_id=None,
                    selected_loop=None,
                    surface="echo_signature",
                    sessions_repo=repos["sessions"],
                    loop_state_repo=repos["loop_states"],
                    completions_repo=repos["completions"],
                    prefs_repo=repos["prefs"],
                    settings=_settings(),
                    now=NOW,
                )
            )

    def test_selected_loop_not_in_snapshot_raises(self, repos):
        _put_session(repos)
        _put_loop(repos, loop_id="pressure")
        with pytest.raises(NoActiveLoops):
            asyncio.run(
                recommend(
                    user_id="u1",
                    session_id=None,
                    selected_loop="grief",
                    surface="mirror_moment",
                    sessions_repo=repos["sessions"],
                    loop_state_repo=repos["loop_states"],
                    completions_repo=repos["completions"],
                    prefs_repo=repos["prefs"],
                    settings=_settings(),
                    now=NOW,
                )
            )


class TestFallbackPaths:
    def test_grief_rising_no_rule_returns_fallback(self, repos):
        # The headline regression: grief rising has no rule. Fallback fires.
        _put_session(repos)
        _put_loop(
            repos,
            loop_id="grief",
            tone_state="rising",
            intensity_score=0.80,
        )
        result = asyncio.run(
            recommend(
                user_id="u1",
                session_id=None,
                selected_loop="grief",
                surface="mirror_moment",
                sessions_repo=repos["sessions"],
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                prefs_repo=repos["prefs"],
                settings=_settings(fallback_enabled=True),
                now=NOW,
            )
        )
        assert result.rule_id == "fallback"
        assert result.practice.id == "breath_4_6"

    def test_grief_rising_with_no_breathwork_returns_alternate(self, repos):
        _put_session(repos)
        _put_loop(
            repos,
            loop_id="grief",
            tone_state="rising",
            intensity_score=0.80,
        )
        # User has no_breathwork=true → fallback swaps to name_and_need.
        asyncio.run(repos["prefs"].set_flags("u1", no_breathwork=True))
        result = asyncio.run(
            recommend(
                user_id="u1",
                session_id=None,
                selected_loop="grief",
                surface="mirror_moment",
                sessions_repo=repos["sessions"],
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                prefs_repo=repos["prefs"],
                settings=_settings(fallback_enabled=True),
                now=NOW,
            )
        )
        assert result.rule_id == "fallback"
        assert result.practice.id == "name_and_need"

    def test_grief_rising_no_rule_no_fallback_raises(self, repos):
        _put_session(repos)
        _put_loop(
            repos,
            loop_id="grief",
            tone_state="rising",
            intensity_score=0.80,
        )
        with pytest.raises(NoRuleMatched):
            asyncio.run(
                recommend(
                    user_id="u1",
                    session_id=None,
                    selected_loop="grief",
                    surface="mirror_moment",
                    sessions_repo=repos["sessions"],
                    loop_state_repo=repos["loop_states"],
                    completions_repo=repos["completions"],
                    prefs_repo=repos["prefs"],
                    settings=_settings(fallback_enabled=False),
                    now=NOW,
                )
            )

    def test_all_candidates_filtered_with_fallback_returns_fallback(self, repos):
        _put_session(repos)
        _put_loop(repos, loop_id="pressure", intensity_score=0.74)

        # Mark every pressure_loop_v1 candidate as recently completed.
        for pid in [
            "breath_4_6",
            "reappraisal_alt_intent",
            "one_percent_first_sentence",
        ]:
            asyncio.run(
                repos["completions"].put(
                    PracticeCompletion(
                        user_id="u1",
                        session_id="s1",
                        loop_id="pressure",
                        tone_state="rising",
                        practice_id=pid,
                        rule_id="pressure_loop_v1",
                        completed_at=(NOW - timedelta(hours=1))
                        .isoformat()
                        .replace("+00:00", "Z"),
                    )
                )
            )

        # The fallback default (breath_4_6) is also recently completed → swap to alt
        # only when no_breathwork. Since no_breathwork=False here, fallback hits
        # the cooldown for breath_4_6 → FallbackOnCooldown.
        with pytest.raises(FallbackOnCooldown):
            asyncio.run(
                recommend(
                    user_id="u1",
                    session_id=None,
                    selected_loop=None,
                    surface="echo_signature",
                    sessions_repo=repos["sessions"],
                    loop_state_repo=repos["loop_states"],
                    completions_repo=repos["completions"],
                    prefs_repo=repos["prefs"],
                    settings=_settings(fallback_enabled=True),
                    now=NOW,
                )
            )

    def test_all_candidates_filtered_no_fallback_raises_409(self, repos):
        _put_session(repos)
        _put_loop(repos, loop_id="pressure", intensity_score=0.74)
        for pid in [
            "breath_4_6",
            "reappraisal_alt_intent",
            "one_percent_first_sentence",
        ]:
            asyncio.run(
                repos["completions"].put(
                    PracticeCompletion(
                        user_id="u1",
                        session_id="s1",
                        loop_id="pressure",
                        tone_state="rising",
                        practice_id=pid,
                        rule_id="pressure_loop_v1",
                        completed_at=(NOW - timedelta(hours=1))
                        .isoformat()
                        .replace("+00:00", "Z"),
                    )
                )
            )
        with pytest.raises(AllCandidatesFiltered):
            asyncio.run(
                recommend(
                    user_id="u1",
                    session_id=None,
                    selected_loop=None,
                    surface="echo_signature",
                    sessions_repo=repos["sessions"],
                    loop_state_repo=repos["loop_states"],
                    completions_repo=repos["completions"],
                    prefs_repo=repos["prefs"],
                    settings=_settings(fallback_enabled=False),
                    now=NOW,
                )
            )


class TestSelectedLoopOverride:
    def test_selected_loop_overrides_top_of_snapshot(self, repos):
        _put_session(repos)
        # pressure higher intensity than overwhelm.
        _put_loop(repos, loop_id="pressure", intensity_score=0.85)
        _put_loop(repos, loop_id="overwhelm", intensity_score=0.70, tone_state="rising")

        result = asyncio.run(
            recommend(
                user_id="u1",
                session_id=None,
                selected_loop="overwhelm",
                surface="mirror_moment",
                sessions_repo=repos["sessions"],
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                prefs_repo=repos["prefs"],
                settings=_settings(),
                now=NOW,
            )
        )
        assert result.pattern.loop_id == "overwhelm"
        assert result.rule_id == "overwhelm_v1"


class TestUnsupportedLoop:
    def test_unsupported_loop_raises(self, repos):
        _put_session(repos)
        _put_loop(repos, loop_id="pressure")
        with pytest.raises(LoopNotSupported):
            asyncio.run(
                recommend(
                    user_id="u1",
                    session_id=None,
                    selected_loop="clarity",  # not V1
                    surface="echo_signature",
                    sessions_repo=repos["sessions"],
                    loop_state_repo=repos["loop_states"],
                    completions_repo=repos["completions"],
                    prefs_repo=repos["prefs"],
                    settings=_settings(),
                    now=NOW,
                )
            )


class TestPrivateModeFlag:
    def test_private_mode_active_echoes_to_response(self, repos):
        _put_session(repos)
        _put_loop(repos, loop_id="pressure", intensity_score=0.74)
        asyncio.run(repos["prefs"].set_flags("u1", private_mode=True))

        result = asyncio.run(
            recommend(
                user_id="u1",
                session_id=None,
                selected_loop=None,
                surface="echo_signature",
                sessions_repo=repos["sessions"],
                loop_state_repo=repos["loop_states"],
                completions_repo=repos["completions"],
                prefs_repo=repos["prefs"],
                settings=_settings(),
                now=NOW,
            )
        )
        assert result.private_mode_active is True
