"""End-to-end coverage of every Reflection Room V1 error code (spec §12).

Each test fires a request that triggers one specific custom exception class
and asserts the central error handler produces:
  * the right HTTP status code
  * an envelope with ``errorCode`` set
  * ``Retry-After`` header on 409s where applicable
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from src.app.api.echo_v1_routes import (
    get_echo_loop_state_repo,
    get_practice_completion_repo,
    get_reflection_session_repo,
    get_telemetry_emitter,
    get_user_personalization_repo,
)

# Each route module declares its own dependency factory; override them all.
from src.app.api.reflection_routes import (
    get_echo_loop_state_repo as get_echo_loop_state_repo_reflection,
)
from src.app.api.reflection_routes import (
    get_reflection_session_repo as get_reflection_session_repo_reflection,
)
from src.app.core.security import get_current_user
from src.app.handler import app
from src.app.models.echo_loop_state import EchoLoopState
from src.app.models.practice_completion import PracticeCompletion
from src.app.models.reflection_session import ReflectionSession
from src.app.repositories.echo_loop_state_repo import EchoLoopStateRepo
from src.app.repositories.practice_completion_repo import PracticeCompletionRepo
from src.app.repositories.reflection_session_repo import (
    GSI_USER_CREATED,
    ReflectionSessionRepo,
)
from src.app.repositories.user_personalization_repo import UserPersonalizationRepo
from src.app.services.practice import settings_loader
from src.app.services.reflection import _config_io
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

SESSIONS = "mc_reflection_sessions-test"
LOOPS = "mc_echo_loop_state-test"
COMPLETIONS = "mc_practice_completions-test"
PREFS = "mc_user_personalization-test"


async def _fake_user() -> Dict[str, Any]:
    return {"id": "test-user-123", "sub": "test-user-123"}


class _NoOpEmitter:
    def emit(self, *_args, **_kwargs):
        return None


@pytest.fixture
def fake_tables():
    return {
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


@pytest.fixture
def repos(fake_tables, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DYNAMODB_REFLECTION_SESSIONS_TABLE", SESSIONS)
    monkeypatch.setenv("DYNAMODB_ECHO_LOOP_STATE_TABLE", LOOPS)
    monkeypatch.setenv("DYNAMODB_PRACTICE_COMPLETIONS_TABLE", COMPLETIONS)
    monkeypatch.setenv("DYNAMODB_USER_PERSONALIZATION_TABLE", PREFS)
    sess = FakeAioSession(fake_tables)
    return {
        "sessions": ReflectionSessionRepo(session=sess),
        "loop_states": EchoLoopStateRepo(session=sess),
        "completions": PracticeCompletionRepo(session=sess),
        "prefs": UserPersonalizationRepo(session=sess),
    }


@pytest.fixture
def client(repos) -> Iterator[TestClient]:
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[get_reflection_session_repo] = lambda: repos["sessions"]
    app.dependency_overrides[get_reflection_session_repo_reflection] = lambda: repos[
        "sessions"
    ]
    app.dependency_overrides[get_echo_loop_state_repo] = lambda: repos["loop_states"]
    app.dependency_overrides[get_echo_loop_state_repo_reflection] = lambda: repos[
        "loop_states"
    ]
    app.dependency_overrides[get_practice_completion_repo] = lambda: repos[
        "completions"
    ]
    app.dependency_overrides[get_user_personalization_repo] = lambda: repos["prefs"]
    app.dependency_overrides[get_telemetry_emitter] = lambda: _NoOpEmitter()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in (
            get_current_user,
            get_reflection_session_repo,
            get_reflection_session_repo_reflection,
            get_echo_loop_state_repo,
            get_echo_loop_state_repo_reflection,
            get_practice_completion_repo,
            get_user_personalization_repo,
            get_telemetry_emitter,
        ):
            app.dependency_overrides.pop(dep, None)


def _seed_session(repos, **overrides) -> ReflectionSession:
    base = dict(
        user_id="test-user-123",
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


def _seed_loop(repos, **overrides) -> EchoLoopState:
    base = dict(
        user_id="test-user-123",
        loop_id="pressure",
        tone_state="rising",
        intensity_score=0.74,
        intensity_label="High",
    )
    base.update(overrides)
    state = EchoLoopState(**base)
    asyncio.run(repos["loop_states"].upsert(state))
    return state


# ============================================================
# 400 INVALID_QUIZ_ANSWER
# ============================================================


def test_invalid_quiz_answer_returns_422_pydantic(client):
    """Pydantic Literal mismatch beats our handler — yields 422 with field
    errors. The custom INVALID_QUIZ_ANSWER (400) only fires from server-side
    paths (e.g. quiz_scorer if config is misshapen)."""
    response = client.post(
        "/api/reflection/quiz",
        json={
            "answers": {
                "q1": "purple",
                "q2": "inspiration",
                "q3": "spiral",
                "q4": "insight",
            }
        },
    )
    assert response.status_code == 422


# ============================================================
# 400 LOOP_NOT_SUPPORTED — also fronted by Pydantic Literal in routes;
# the recommender raises this server-side if hit through other paths.
# ============================================================


def test_loop_not_supported_returns_422(client, repos):
    session = _seed_session(repos)
    _seed_loop(repos)
    response = client.post(
        "/api/echo/recommend-practice",
        json={"session_id": session.session_id, "selected_loop": "clarity"},
    )
    assert response.status_code == 422


# ============================================================
# 400 MOTIF_NOT_FOUND
# ============================================================


def test_motif_not_found_returns_400(client, repos):
    """PUT /me/reflection/room with an unknown motif_id."""
    session = _seed_session(
        repos,
        motif_payload={"override_allowed": True},
    )
    response = client.put(
        "/api/me/reflection/room",
        json={"motif_id": "banana", "apply_to": "session"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["errorCode"] == "MOTIF_NOT_FOUND"


# ============================================================
# 403 OVERRIDE_NOT_ALLOWED
# ============================================================


def test_override_not_allowed_returns_403(client, repos):
    _seed_session(repos, motif_payload={"override_allowed": False})
    response = client.put(
        "/api/me/reflection/room",
        json={"motif_id": "mirror", "apply_to": "session"},
    )
    assert response.status_code == 403
    assert response.json()["errorCode"] == "OVERRIDE_NOT_ALLOWED"


# ============================================================
# 404 NO_ACTIVE_LOOPS
# ============================================================


def test_no_active_loops_returns_404(client, repos):
    session = _seed_session(repos)
    _seed_loop(
        repos,
        loop_id="pressure",
        intensity_score=0.3,
        tone_state="rising",
        recently_changed=False,
    )
    response = client.post(
        "/api/echo/recommend-practice",
        json={"session_id": session.session_id},
    )
    assert response.status_code == 404
    assert response.json()["errorCode"] == "NO_ACTIVE_LOOPS"


# ============================================================
# 404 NO_RULE_MATCHED — only when fallback_enabled=False
# ============================================================


def test_no_rule_matched_returns_404_when_fallback_disabled(
    client, repos, monkeypatch: pytest.MonkeyPatch
):
    # Patch the settings loader to flip fallback_enabled off.
    from src.app.services.practice.settings_loader import (
        MicroPracticeDefaults,
        MicroPracticeSettings,
    )

    disabled = MicroPracticeSettings(
        version=1,
        defaults=MicroPracticeDefaults(
            cooldown_hours_default=12,
            cooldown_hours_grief=24,
            fallback_enabled=False,
        ),
    )
    monkeypatch.setattr(
        "src.app.services.practice.recommender.load_micro_practice_settings",
        lambda: disabled,
    )

    session = _seed_session(repos)
    _seed_loop(repos, loop_id="grief", intensity_score=0.80, tone_state="rising")

    response = client.post(
        "/api/echo/recommend-practice",
        json={"session_id": session.session_id, "selected_loop": "grief"},
    )
    assert response.status_code == 404
    assert response.json()["errorCode"] == "NO_RULE_MATCHED"


# ============================================================
# 409 ALL_CANDIDATES_FILTERED — only when fallback_enabled=False
# ============================================================


def test_all_candidates_filtered_returns_409_with_retry_after(
    client, repos, monkeypatch: pytest.MonkeyPatch
):
    from src.app.services.practice.settings_loader import (
        MicroPracticeDefaults,
        MicroPracticeSettings,
    )

    disabled = MicroPracticeSettings(
        version=1,
        defaults=MicroPracticeDefaults(
            cooldown_hours_default=12,
            cooldown_hours_grief=24,
            fallback_enabled=False,
        ),
    )
    monkeypatch.setattr(
        "src.app.services.practice.recommender.load_micro_practice_settings",
        lambda: disabled,
    )

    session = _seed_session(repos)
    _seed_loop(repos, loop_id="pressure", intensity_score=0.74)

    # Mark every pressure_loop_v1 candidate as recently completed.
    recent = (
        (datetime.now(timezone.utc) - timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    for pid in ("breath_4_6", "reappraisal_alt_intent", "one_percent_first_sentence"):
        asyncio.run(
            repos["completions"].put(
                PracticeCompletion(
                    user_id="test-user-123",
                    session_id=session.session_id,
                    loop_id="pressure",
                    tone_state="rising",
                    practice_id=pid,
                    rule_id="pressure_loop_v1",
                    completed_at=recent,
                )
            )
        )

    response = client.post(
        "/api/echo/recommend-practice",
        json={"session_id": session.session_id},
    )
    assert response.status_code == 409
    assert response.json()["errorCode"] == "ALL_CANDIDATES_FILTERED"
    assert response.headers.get("Retry-After")
    assert int(response.headers["Retry-After"]) > 0


# ============================================================
# 409 FALLBACK_ON_COOLDOWN — fallback fired but breath_4_6 on cooldown
# ============================================================


def test_fallback_on_cooldown_returns_409_with_retry_after(client, repos):
    session = _seed_session(repos)
    _seed_loop(repos, loop_id="pressure", intensity_score=0.74)

    # All pressure candidates AND fallback's breath_4_6 are recently completed.
    recent = (
        (datetime.now(timezone.utc) - timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    for pid in ("breath_4_6", "reappraisal_alt_intent", "one_percent_first_sentence"):
        asyncio.run(
            repos["completions"].put(
                PracticeCompletion(
                    user_id="test-user-123",
                    session_id=session.session_id,
                    loop_id="pressure",
                    tone_state="rising",
                    practice_id=pid,
                    rule_id="pressure_loop_v1",
                    completed_at=recent,
                )
            )
        )

    response = client.post(
        "/api/echo/recommend-practice",
        json={"session_id": session.session_id},
    )
    assert response.status_code == 409
    assert response.json()["errorCode"] == "FALLBACK_ON_COOLDOWN"
    assert response.headers.get("Retry-After")
    assert int(response.headers["Retry-After"]) > 0


# ============================================================
# 409 OVERRIDE_TAG_NOT_IN_TIE
# ============================================================


def test_override_tag_not_in_tie_returns_409(client, repos):
    """user_override_tag must be one of the tied tags. Construct a quiz that
    ties on alpha vs beta (via the existing rules — the canonical Spiral
    quiz has a clean winner, so we hand-construct a synthetic answer set
    via the seeded session)."""
    # Easier: post a normal quiz first (no tie), then resubmit with an
    # invalid override_tag — but since there's no tie, the route never
    # treats it as a tie. We need a real tie. The closest reproducible tie
    # in the production rules is impossible without changing the YAML.
    #
    # Solution: monkeypatch the scorer to return a tied result, then submit.
    import src.app.api.reflection_routes as rr
    from src.app.services.reflection.quiz_scorer import ScoringResult

    fake_result = ScoringResult(
        winning_tag="evolution",
        override_allowed=True,
        scores={"evolution": 5, "clarity": 5},
        explanation=["fake"],
        tied_tags=["clarity", "evolution"],
    )

    # First submit normally so a session exists with override_allowed=True path.
    # Patch score_quiz used inside the route module so it returns our tied result.
    original_score = rr.score_quiz

    def patched_score(answers, rules, user_override_tag=None):
        if user_override_tag is None:
            return fake_result
        if user_override_tag not in fake_result.tied_tags:
            from src.app.core.exceptions import OverrideTagNotInTie

            raise OverrideTagNotInTie(
                f"override tag '{user_override_tag}' not in tied set "
                f"{fake_result.tied_tags}"
            )
        return ScoringResult(
            winning_tag=user_override_tag,
            override_allowed=False,
            scores=fake_result.scores,
            explanation=fake_result.explanation,
            tied_tags=[],
        )

    rr.score_quiz = patched_score
    try:
        # No prior session — seed via first call (override_allowed=True returned).
        first = client.post(
            "/api/reflection/quiz",
            json={
                "answers": {
                    "q1": "hopeful",
                    "q2": "inspiration",
                    "q3": "spiral",
                    "q4": "insight",
                }
            },
        )
        assert first.status_code == 200
        assert first.json()["data"]["motif"]["override_allowed"] is True

        # Now submit override_tag that isn't in the tied set.
        response = client.post(
            "/api/reflection/quiz",
            json={
                "answers": {
                    "q1": "hopeful",
                    "q2": "inspiration",
                    "q3": "spiral",
                    "q4": "insight",
                },
                "user_override_tag": "growth",
            },
        )
        assert response.status_code == 409
        assert response.json()["errorCode"] == "OVERRIDE_TAG_NOT_IN_TIE"
    finally:
        rr.score_quiz = original_score


# ============================================================
# Smoke test: error envelope has the standard shape on every Reflection error
# ============================================================


def test_envelope_shape_includes_success_error_request_id_timestamp_errorcode(
    client, repos
):
    """Any Reflection-Room exception lands on the central handler — envelope
    must carry the documented fields."""
    session = _seed_session(repos)
    response = client.put(
        "/api/me/reflection/room",
        json={"motif_id": "banana"},
    )
    body = response.json()
    assert body["success"] is False
    assert "error" in body
    assert "requestId" in body
    assert "timestamp" in body
    assert "errorCode" in body
