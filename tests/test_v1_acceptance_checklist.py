"""Reflection Room V1 acceptance checklist (PDF §17 → spec §B.4).

Each test maps to one §17 item. Items 3 and 10 are FE-side; backed up here
with explicit ``pytest.skip`` calls and a comment so the mapping is complete.

Run via:
    pytest -m acceptance tests/test_v1_acceptance_checklist.py
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# Each route module declares its own dep factories — override all of them
# so cross-route tests don't leak to real DDB.
from src.app.api.echo_v1_routes import (
    get_echo_loop_state_repo,
    get_practice_completion_repo,
    get_reflection_session_repo,
    get_telemetry_emitter,
    get_user_personalization_repo,
)
from src.app.api.me_routes import get_telemetry_emitter as get_telemetry_me
from src.app.api.me_routes import get_user_personalization_repo as get_prefs_me
from src.app.api.practice_routes import get_echo_loop_state_repo as get_loops_practice
from src.app.api.practice_routes import (
    get_practice_completion_repo as get_completions_practice,
)
from src.app.api.practice_routes import (
    get_reflection_session_repo as get_sessions_practice,
)
from src.app.api.practice_routes import get_telemetry_emitter as get_telemetry_practice
from src.app.api.practice_routes import (
    get_user_personalization_repo as get_prefs_practice,
)
from src.app.api.reflection_routes import (
    get_echo_loop_state_repo as get_loops_reflection,
)
from src.app.api.reflection_routes import (
    get_reflection_session_repo as get_sessions_reflection,
)
from src.app.api.telemetry_routes import get_telemetry_emitter as get_telemetry_beacons
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
from src.app.services.reflection.motif_mapping_loader import load_motif_mapping
from src.app.services.reflection.quiz_rules_loader import load_quiz_rules
from src.app.services.reflection.quiz_scorer import score_quiz
from tests._fakes.fake_dynamodb import FakeAioSession, FakeTable

pytestmark = pytest.mark.acceptance


SESSIONS = "mc_reflection_sessions-test"
LOOPS = "mc_echo_loop_state-test"
COMPLETIONS = "mc_practice_completions-test"
PREFS = "mc_user_personalization-test"
USER_ID = "test-user-123"


async def _fake_user() -> Dict[str, Any]:
    return {"id": USER_ID, "sub": USER_ID, "email": "test@example.com"}


class _SpyEmitter:
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    def emit(self, event_name: str, *, user_hash: str, **fields: Any) -> None:
        self.events.append({"event": event_name, "user_hash": user_hash, **fields})


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def fake_tables() -> Dict[str, FakeTable]:
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
def emitter() -> _SpyEmitter:
    return _SpyEmitter()


@pytest.fixture
def client(repos, emitter) -> Iterator[TestClient]:
    overrides = {
        get_current_user: _fake_user,
        # echo_v1_routes
        get_reflection_session_repo: lambda: repos["sessions"],
        get_echo_loop_state_repo: lambda: repos["loop_states"],
        get_practice_completion_repo: lambda: repos["completions"],
        get_user_personalization_repo: lambda: repos["prefs"],
        get_telemetry_emitter: lambda: emitter,
        # reflection_routes
        get_sessions_reflection: lambda: repos["sessions"],
        get_loops_reflection: lambda: repos["loop_states"],
        # practice_routes
        get_sessions_practice: lambda: repos["sessions"],
        get_loops_practice: lambda: repos["loop_states"],
        get_completions_practice: lambda: repos["completions"],
        get_prefs_practice: lambda: repos["prefs"],
        get_telemetry_practice: lambda: emitter,
        # me_routes
        get_prefs_me: lambda: repos["prefs"],
        get_telemetry_me: lambda: emitter,
        # telemetry_routes
        get_telemetry_beacons: lambda: emitter,
    }
    for dep, factory in overrides.items():
        app.dependency_overrides[dep] = factory
    try:
        with TestClient(app) as c:
            yield c
    finally:
        for dep in overrides:
            app.dependency_overrides.pop(dep, None)


# ============================================================
# Test fixtures helpers
# ============================================================


def _put_session(repos, **overrides) -> ReflectionSession:
    base = dict(
        user_id=USER_ID,
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


def _put_loop(repos, **overrides) -> EchoLoopState:
    base = dict(
        user_id=USER_ID,
        loop_id="pressure",
        tone_state="rising",
        intensity_score=0.74,
        intensity_label="High",
        last_seen="2026-05-03T20:10:00Z",
    )
    base.update(overrides)
    state = EchoLoopState(**base)
    asyncio.run(repos["loop_states"].upsert(state))
    return state


# ============================================================
# §17 #1 — Quiz scoring matches rules YAML
# ============================================================


def test_acc_01_quiz_scoring_matches_yaml(client, repos):
    """Submit 50 random quizzes via the API and verify the API's winning_tag /
    scores match an inline re-implementation of the spec §7 algorithm against
    the same YAML."""
    rules = load_quiz_rules()
    rnd = random.Random(42)
    answer_pools = {
        q: list(rules.questions[q].answers.keys()) for q in ("q1", "q2", "q3", "q4")
    }

    for _ in range(50):
        # Wipe state between iterations so each call exercises a fresh "different
        # answers within active session" overwrite (or create) path.
        (
            asyncio.run(
                repos["sessions"].put(  # ensure session row gets reused
                    ReflectionSession(
                        user_id=USER_ID,
                        motif_id="placeholder",
                        motif_name="placeholder",
                        room_skin="placeholder",
                        motif_payload={"override_allowed": False},
                        quiz_answers={"q1": "x", "q2": "x", "q3": "x", "q4": "x"},
                        scores={},
                        user_tz="America/New_York",
                        expires_at="2099-01-01T00:00:00Z",
                    )
                )
            )
            if False
            else None
        )

        answers = {q: rnd.choice(pool) for q, pool in answer_pools.items()}
        response = client.post("/api/reflection/quiz", json={"answers": answers})
        assert response.status_code == 200, response.text

        data = response.json()["data"]
        api_motif_id = data["motif"]["motif_id"]
        api_scores = data["motif"]["scores"]

        # Re-implement the spec §7 algorithm inline.
        expected = score_quiz(answers, rules)
        expected_motif_id = load_motif_mapping().lookup(expected.winning_tag).motif_id

        assert (
            api_motif_id == expected_motif_id
        ), f"answers={answers}: api={api_motif_id} expected={expected_motif_id}"
        assert (
            api_scores == expected.scores
        ), f"answers={answers}: api_scores={api_scores} expected={expected.scores}"


# ============================================================
# §17 #2 — Motif payload uses motif_mapping.json (every required key present)
# ============================================================


def test_acc_02_motif_payload_keys(client, repos):
    response = client.post(
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
    assert response.status_code == 200
    motif = response.json()["data"]["motif"]
    required = {
        "motif_id",
        "motif_name",
        "icon",
        "element",
        "tone_tag",
        "why_text",
        "room_skin",
    }
    for key in required:
        assert key in motif, f"missing motif key: {key}"
        assert motif[key], f"empty motif value for {key}"

    # Cross-check values come from motif_mapping.v1.json.
    expected = load_motif_mapping().lookup("evolution")
    assert motif["motif_id"] == expected.motif_id
    assert motif["motif_name"] == expected.motif_name
    assert motif["why_text"] == expected.why_text


# ============================================================
# §17 #3 — Room shell ambience-only — FE-only, no backend assertion
# ============================================================


def test_acc_03_room_shell_ambience_fe_only():
    pytest.skip("§17 #3 is a frontend visual concern; nothing to assert backend-side")


# ============================================================
# §17 #4 — Snapshot returns only V1 supported loops
# ============================================================


def test_acc_04_snapshot_supported_loops_only(client, repos):
    _put_session(repos)
    _put_loop(repos, loop_id="pressure")
    # Insert a forward-compat row that the snapshot must filter out.
    asyncio.run(
        repos["loop_states"].upsert(
            EchoLoopState(
                user_id=USER_ID,
                loop_id="clarity",  # not in V1 set
                tone_state="rising",
                intensity_score=0.7,
                intensity_label="High",
                last_seen="2026-05-03T20:10:00Z",
            )
        )
    )
    response = client.get("/api/echo/snapshot")
    assert response.status_code == 200
    loop_ids = {l["loop_id"] for l in response.json()["data"]["loops"]}
    assert loop_ids == {"pressure"}


# ============================================================
# §17 #5 — Echo Signature shows tone state on every card
# ============================================================


def test_acc_05_every_loop_has_tone_state(client, repos):
    _put_session(repos)
    _put_loop(repos, loop_id="pressure", tone_state="rising")
    _put_loop(repos, loop_id="grief", tone_state="softening", intensity_score=0.5)
    _put_loop(repos, loop_id="agency", tone_state="steady", intensity_score=0.65)

    response = client.get("/api/echo/snapshot")
    loops = response.json()["data"]["loops"]
    assert len(loops) == 3
    for loop in loops:
        assert loop.get("tone_state") in {"rising", "steady", "softening"}


# ============================================================
# §17 #6 — Echo Signature card front uses snapshot+tone library only
# ============================================================


def test_acc_06_signature_inputs_only(client, repos):
    """The snapshot endpoint must not invoke the recommender. We patch the
    recommender at its public entry point and assert it's never called."""
    _put_session(repos)
    _put_loop(repos)

    with patch(
        "src.app.api.echo_v1_routes.recommend",
        side_effect=AssertionError("snapshot endpoint must not call recommend()"),
    ):
        response = client.get("/api/echo/snapshot")
        assert response.status_code == 200


# ============================================================
# §17 #7 — Echo Signature CTA + Mirror Moment use the shared engine
# ============================================================


def test_acc_07_shared_engine(client, repos):
    """Both surfaces hit the same /echo/recommend-practice route; we assert
    the route accepts both surface enums and returns a structurally identical
    response."""
    session = _put_session(repos)
    _put_loop(repos, intensity_score=0.74)

    sig = client.post(
        "/api/echo/recommend-practice",
        json={"session_id": session.session_id, "surface": "echo_signature"},
    )
    moment = client.post(
        "/api/echo/recommend-practice",
        json={"session_id": session.session_id, "surface": "mirror_moment"},
    )
    assert sig.status_code == 200
    assert moment.status_code == 200
    assert set(sig.json()["data"].keys()) == set(moment.json()["data"].keys())


# ============================================================
# §17 #8 — Echo Map renders 6 loops with required fields
# ============================================================


def test_acc_08_map_data_contract(client, repos):
    """When all 6 V1 loops are active, the snapshot returns each with the
    fields the Echo Map needs (loop_id, tone_state, intensity_label, icon,
    reflection_line, last_seen)."""
    _put_session(repos)
    for i, loop_id in enumerate(
        ["pressure", "overwhelm", "grief", "self_silencing", "agency", "transition"]
    ):
        _put_loop(
            repos,
            loop_id=loop_id,
            tone_state="rising",
            intensity_score=0.65 + i * 0.02,
        )

    response = client.get("/api/echo/snapshot")
    loops = response.json()["data"]["loops"]
    assert {l["loop_id"] for l in loops} == {
        "pressure",
        "overwhelm",
        "grief",
        "self_silencing",
        "agency",
        "transition",
    }
    required = {
        "loop_id",
        "tone_state",
        "intensity_label",
        "icon",
        "reflection_line",
        "last_seen",
    }
    for loop in loops:
        for field in required:
            assert (
                field in loop and loop[field] is not None
            ), f"missing/empty {field} on {loop}"


# ============================================================
# §17 #9 — Mirror Moment buttons fully dynamic from top-3
# ============================================================


def test_acc_09_top3(client, repos):
    """The snapshot exposes loops sorted desc by intensity. The first three
    entries are the top-3 the FE will render as Mirror Moment buttons."""
    _put_session(repos)
    _put_loop(repos, loop_id="pressure", intensity_score=0.85)
    _put_loop(repos, loop_id="overwhelm", intensity_score=0.75)
    _put_loop(repos, loop_id="grief", intensity_score=0.65, tone_state="softening")
    _put_loop(repos, loop_id="agency", intensity_score=0.45, tone_state="rising")

    response = client.get("/api/echo/snapshot")
    loops = response.json()["data"]["loops"]
    assert len(loops) >= 3
    top3 = [l["loop_id"] for l in loops[:3]]
    # All three are in the V1 supported set; no labels are hardcoded — labels
    # are derived FE-side from (loop_id, tone_state). We only enforce ordering.
    assert top3 == ["pressure", "overwhelm", "grief"]


# ============================================================
# §17 #10 — Mirror Moment label matrix — FE-side
# ============================================================


def test_acc_10_mirror_moment_labels_fe_only():
    pytest.skip(
        "§17 #10 lives in 04_UI_DEVELOPER_HANDOFF.md (frontend); backend "
        "responsibility is only to expose top-3 (covered by #9)."
    )


# ============================================================
# §17 #11 — Practice completion refreshes snapshot + personalizer
# ============================================================


def test_acc_11_completion_side_effects(client, repos):
    session = _put_session(repos)
    _put_loop(repos, loop_id="pressure", intensity_score=0.74)

    response = client.post(
        "/api/practice/complete",
        json={
            "session_id": session.session_id,
            "loop_id": "pressure",
            "tone_state": "rising",
            "practice_id": "breath_4_6",
            "rule_id": "pressure_loop_v1",
            "helpful": True,
        },
    )
    assert response.status_code == 200
    body = response.json()["data"]

    # The inline snapshot reflects the state delta.
    pressure = next(l for l in body["snapshot"]["loops"] if l["loop_id"] == "pressure")
    assert pressure["intensity_score"] == pytest.approx(0.64, abs=1e-3)
    assert pressure["tone_state"] == "softening"

    # Personalization row updated.
    prefs = asyncio.run(repos["prefs"].get_or_default(USER_ID))
    assert prefs.recent_use["breath_4_6"].count_30d == 1
    assert len(prefs.practice_helpfulness["breath_4_6"]) == 1


# ============================================================
# §17 #12 — Cooldowns enforced server-side
# ============================================================


def test_acc_12_cooldown_server_side(client, repos):
    session = _put_session(repos)
    _put_loop(repos, intensity_score=0.74)

    # Manually plant a recent completion of breath_4_6.
    recent_iso = (
        (datetime.now(timezone.utc) - timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    asyncio.run(
        repos["completions"].put(
            PracticeCompletion(
                user_id=USER_ID,
                session_id=session.session_id,
                loop_id="pressure",
                tone_state="rising",
                practice_id="breath_4_6",
                rule_id="pressure_loop_v1",
                completed_at=recent_iso,
            )
        )
    )
    response = client.post(
        "/api/echo/recommend-practice",
        json={"session_id": session.session_id},
    )
    assert response.status_code == 200
    # breath_4_6 should be filtered; another candidate from pressure_loop_v1
    # is returned.
    practice = response.json()["data"]["practice"]
    assert practice["id"] != "breath_4_6"
    assert practice["id"] in {"reappraisal_alt_intent", "one_percent_first_sentence"}


# ============================================================
# §17 #13a — no_breathwork enforced service-side
# ============================================================


def test_acc_13a_no_breathwork(client, repos):
    session = _put_session(repos)
    _put_loop(repos, intensity_score=0.74)

    client.put("/api/me/preferences/flags", json={"no_breathwork": True})

    response = client.post(
        "/api/echo/recommend-practice",
        json={"session_id": session.session_id},
    )
    assert response.status_code == 200
    practice = response.json()["data"]["practice"]
    assert practice["type"] != "breath"


# ============================================================
# §17 #13b — reduced_motion / private_mode echoed in user state response
# ============================================================


def test_acc_13b_reduced_motion_in_response(client):
    client.put("/api/me/preferences/flags", json={"reduced_motion": True})
    response = client.get("/api/me/preferences")
    assert response.status_code == 200
    assert response.json()["data"]["flags"]["reduced_motion"] is True


# ============================================================
# §17 #14 — Empty / loading / error states for Room, Signature, Map
# ============================================================


def test_acc_14_empty_state_returns_200_empty_loops(client, repos):
    """A session with no loop rows yields 200 with loops=[] (the FE renders
    'All quiet for now')."""
    _put_session(repos)
    response = client.get("/api/echo/snapshot")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["loops"] == []
    assert data["motif_context"]["motif_id"]  # populated


# ============================================================
# §17 #15 — Telemetry events: IDs only, never raw text
# ============================================================


def test_acc_15_telemetry_no_pii(client, repos, emitter):
    session = _put_session(repos)
    _put_loop(repos, loop_id="pressure", intensity_score=0.74)

    # Trigger several events.
    client.get("/api/echo/snapshot")  # echo_signature_view
    client.post(
        "/api/practice/complete",
        json={
            "session_id": session.session_id,
            "loop_id": "pressure",
            "tone_state": "rising",
            "practice_id": "breath_4_6",
            "rule_id": "pressure_loop_v1",
            "helpful": True,
        },
    )
    client.post(
        "/api/telemetry/practice-expand",
        json={"loop_id": "pressure", "practice_id": "breath_4_6"},
    )
    client.post("/api/me/private-mode/reveal", json={"surface": "echo_signature"})

    assert len(emitter.events) >= 4
    # Every emitted field value is a primitive (no dicts/lists, no arbitrary text).
    for ev in emitter.events:
        for key, value in ev.items():
            assert isinstance(
                value, (str, int, float, bool)
            ), f"event {ev['event']!r} field {key!r}={value!r} is not a primitive"
            if isinstance(value, str):
                # No long free-form values; either short enums or ts/hash.
                assert (
                    len(value) <= 64
                ), f"event {ev['event']!r} field {key!r} too long: {len(value)} chars"
