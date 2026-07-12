"""Unit tests for SoulPingService — config resolution, throttle, and the
per-user orchestration (generate → send), driven entirely through injected
mocks (configured before injection, per the repo's mypy-safe mock pattern)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.app.models.soul_ping import SoulPing, SoulPingCategory
from src.app.models.user_profile import UserProfile
from src.app.services import soul_ping_service as sps
from src.app.services.soul_ping_service import SoulPingService


def _profile(prefs=None) -> UserProfile:
    return UserProfile(user_id="u1", email="u1@example.com", preferences=prefs)


def _ping(sent_at: str) -> SoulPing:
    return SoulPing(
        user_id="u1",
        category=SoulPingCategory.EMOTIONAL,
        title="t",
        body="b",
        sent_at=sent_at,
    )


def _build(db=None, openai=None, conv=None, sns=None) -> SoulPingService:
    """Construct a service from (pre-configured) mocks."""
    return SoulPingService(
        dynamodb_service=db or AsyncMock(),
        openai_service=openai or AsyncMock(),
        conversation_service=conv or AsyncMock(),
        sns_service=sns or AsyncMock(),
    )


def _convo():
    return SimpleNamespace(
        summary="Working through stress at work.",
        key_themes=["stress", "avoidance"],
        open_threads=["hasn't decided about the project"],
        conversation_id="c1",
    )


# --------------------------------------------------------------------- config
def test_get_config_defaults_to_enabled_all_categories():
    config = SoulPingService.get_config(_profile())
    assert config["enabled"] is True
    assert set(config["categories"]) == {"emotional", "progress", "systemic"}


def test_get_config_respects_disabled_and_subset():
    config = SoulPingService.get_config(
        _profile({"soul_pings": {"enabled": False, "categories": ["emotional"]}})
    )
    assert config["enabled"] is False
    assert config["categories"] == ["emotional"]


def test_get_config_filters_unknown_categories():
    config = SoulPingService.get_config(
        _profile({"soul_pings": {"categories": ["emotional", "bogus", "goal"]}})
    )
    # 'bogus' is junk; 'goal' is not a v1 category — both dropped.
    assert config["categories"] == ["emotional"]


# ----------------------------------------------------------------- json parse
def test_extract_json_handles_wrapped_output():
    text = 'Sure!\n```json\n{"category":"emotional","title":"Hi","body":"x"}\n```'
    assert sps._extract_json(text) == {
        "category": "emotional",
        "title": "Hi",
        "body": "x",
    }


def test_extract_json_returns_none_on_garbage():
    assert sps._extract_json("no json here") is None


# -------------------------------------------------------------------- throttle
async def test_was_pinged_recently_true_within_window():
    db = AsyncMock()
    recent = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    db.get_last_soul_ping = AsyncMock(return_value=_ping(recent))
    assert await _build(db=db).was_pinged_recently("u1") is True


async def test_was_pinged_recently_false_outside_window():
    db = AsyncMock()
    old = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    db.get_last_soul_ping = AsyncMock(return_value=_ping(old))
    assert await _build(db=db).was_pinged_recently("u1") is False


async def test_was_pinged_recently_false_when_none():
    db = AsyncMock()
    db.get_last_soul_ping = AsyncMock(return_value=None)
    assert await _build(db=db).was_pinged_recently("u1") is False


# ---------------------------------------------------------------- orchestrate
async def test_maybe_send_skips_when_disabled():
    db = AsyncMock()
    db.get_user_profile = AsyncMock(
        return_value=_profile({"soul_pings": {"enabled": False}})
    )
    result = await _build(db=db).maybe_send_for_user("u1")
    assert result.status == "skipped" and result.reason == "disabled"


async def test_maybe_send_skips_when_throttled():
    db = AsyncMock()
    db.get_user_profile = AsyncMock(return_value=_profile())
    recent = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    db.get_last_soul_ping = AsyncMock(return_value=_ping(recent))
    result = await _build(db=db).maybe_send_for_user("u1")
    assert result.status == "skipped" and result.reason == "throttled"


async def test_maybe_send_skips_no_content_when_no_conversation():
    db = AsyncMock()
    db.get_user_profile = AsyncMock(return_value=_profile())
    db.get_last_soul_ping = AsyncMock(return_value=None)
    conv = AsyncMock()
    conv.get_recent_conversations = AsyncMock(return_value=[])  # nothing to say
    result = await _build(db=db, conv=conv).maybe_send_for_user("u1")
    assert result.status == "skipped" and result.reason == "no_content"


async def test_maybe_send_force_bypasses_throttle():
    """force=True must still generate, but must NOT be blocked by the throttle."""
    db = AsyncMock()
    db.get_user_profile = AsyncMock(return_value=_profile())
    recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db.get_last_soul_ping = AsyncMock(return_value=_ping(recent))  # would block
    conv = AsyncMock()
    conv.get_recent_conversations = AsyncMock(
        return_value=[]
    )  # → no_content, not throttled
    result = await _build(db=db, conv=conv).maybe_send_for_user("u1", force=True)
    assert result.reason == "no_content"  # reached generation, not "throttled"


async def test_maybe_send_happy_path_generates_and_sends():
    db = AsyncMock()
    db.get_user_profile = AsyncMock(return_value=_profile())
    db.get_last_soul_ping = AsyncMock(return_value=None)
    db.get_user_device_tokens = AsyncMock(
        return_value=[{"endpoint_arn": "arn:1", "is_active": True}]
    )
    db.save_soul_ping = AsyncMock(return_value=True)

    conv = AsyncMock()
    conv.get_recent_conversations = AsyncMock(return_value=[_convo()])
    conv.get_conversation_history = AsyncMock(return_value=[])

    openai = AsyncMock()
    openai.send_with_overrides_async = AsyncMock(
        return_value='{"category":"systemic","title":"A pattern","body":"You keep circling stress."}'
    )

    sns = AsyncMock()
    sns.publish_to_endpoint_async = AsyncMock(return_value="msg-1")

    result = await _build(db=db, openai=openai, conv=conv, sns=sns).maybe_send_for_user(
        "u1"
    )
    assert result.status == "sent"
    assert result.category == "systemic"
    assert result.endpoints == 1
    sns.publish_to_endpoint_async.assert_awaited_once()
    db.save_soul_ping.assert_awaited_once()


async def test_generate_falls_back_to_enabled_category_on_bad_llm_category():
    db = AsyncMock()
    conv = AsyncMock()
    conv.get_recent_conversations = AsyncMock(return_value=[_convo()])
    conv.get_conversation_history = AsyncMock(return_value=[])
    openai = AsyncMock()
    # LLM returns a category the user hasn't enabled → fall back to first enabled.
    openai.send_with_overrides_async = AsyncMock(
        return_value='{"category":"progress","title":"Hi","body":"hello there"}'
    )
    svc = _build(db=db, openai=openai, conv=conv)
    ping = await svc.generate_ping("u1", ["emotional"])
    assert ping is not None
    assert ping.category == SoulPingCategory.EMOTIONAL


async def test_send_and_record_skips_when_no_endpoints():
    db = AsyncMock()
    db.get_user_device_tokens = AsyncMock(return_value=[])
    ping = SoulPing(
        user_id="u1", category=SoulPingCategory.EMOTIONAL, title="t", body="b"
    )
    assert await _build(db=db).send_and_record(ping) == 0


def test_push_data_is_all_strings():
    data = SoulPing(
        user_id="u1", category=SoulPingCategory.EMOTIONAL, title="t", body="b"
    ).push_data()
    assert data["type"] == "soul_ping"
    assert data["category"] == "emotional"
    assert all(isinstance(v, str) for v in data.values())


# ------------------------------------------------- seen / re-engagement (2026)
def _iso(dt) -> str:
    return dt.isoformat()


def _seen_ping(sent_at: str) -> SoulPing:
    p = _ping(sent_at)
    p.read_at = "2026-07-12T00:00:00Z"
    return p


def test_pick_reengagement_differs_from_last():
    first_title, first_body = sps._REENGAGEMENT_PINGS[0]
    # If the last message was that first variant, we rotate to a different one.
    assert sps._pick_reengagement(first_body) != (first_title, first_body)
    # A non-re-engagement (content) last body → start at the first variant.
    assert sps._pick_reengagement("some content body") == sps._REENGAGEMENT_PINGS[0]


def test_build_reengagement_ping_falls_back_to_enabled_category():
    ping = _build().build_reengagement_ping("u1", ["progress"], _ping("x"))
    assert ping.category.value == "progress"  # emotional not enabled → first enabled
    assert ping.body in [b for _t, b in sps._REENGAGEMENT_PINGS]


async def test_has_new_activity_true_when_conversation_newer():
    now = datetime.now(timezone.utc)
    last = _ping(_iso(now - timedelta(hours=2)))
    conv = AsyncMock()
    conv.get_recent_conversations = AsyncMock(
        return_value=[SimpleNamespace(last_message_at=_iso(now - timedelta(minutes=1)))]
    )
    assert await _build(conv=conv)._has_new_activity_since("u1", last) is True


async def test_has_new_activity_false_when_conversation_older():
    now = datetime.now(timezone.utc)
    last = _ping(_iso(now))
    conv = AsyncMock()
    conv.get_recent_conversations = AsyncMock(
        return_value=[SimpleNamespace(last_message_at=_iso(now - timedelta(hours=1)))]
    )
    assert await _build(conv=conv)._has_new_activity_since("u1", last) is False


async def test_maybe_send_reengages_when_seen_and_no_new_activity():
    """Seen last ping + no new reflection → a DIFFERENT re-engagement nudge,
    no LLM, and not a duplicate of the prior content."""
    now = datetime.now(timezone.utc)
    db = AsyncMock()
    db.get_user_profile = AsyncMock(return_value=_profile())
    db.get_last_soul_ping = AsyncMock(
        return_value=_seen_ping(_iso(now - timedelta(hours=2)))
    )
    db.get_user_device_tokens = AsyncMock(
        return_value=[{"endpoint_arn": "arn:1", "is_active": True}]
    )
    db.save_soul_ping = AsyncMock(return_value=True)
    conv = AsyncMock()
    conv.get_recent_conversations = AsyncMock(
        return_value=[SimpleNamespace(last_message_at=_iso(now - timedelta(hours=5)))]
    )
    openai = AsyncMock()
    sns = AsyncMock()
    sns.publish_to_endpoint_async = AsyncMock(return_value="m")

    result = await _build(db=db, openai=openai, conv=conv, sns=sns).maybe_send_for_user(
        "u1"
    )

    assert result.status == "sent"
    openai.send_with_overrides_async.assert_not_called()  # re-engagement = no LLM
    assert db.save_soul_ping.await_args is not None
    saved = db.save_soul_ping.await_args.args[0]
    assert saved.body != "b"  # not the old content body
    assert saved.body in [b for _t, b in sps._REENGAGEMENT_PINGS]


async def test_maybe_send_skips_when_unseen_and_no_new_activity():
    """Unseen last ping + no new reflection → skip (no duplicate stacking)."""
    now = datetime.now(timezone.utc)
    db = AsyncMock()
    db.get_user_profile = AsyncMock(return_value=_profile())
    db.get_last_soul_ping = AsyncMock(  # read_at is None → unseen
        return_value=_ping(_iso(now - timedelta(hours=2)))
    )
    conv = AsyncMock()
    conv.get_recent_conversations = AsyncMock(
        return_value=[SimpleNamespace(last_message_at=_iso(now - timedelta(hours=5)))]
    )
    result = await _build(db=db, conv=conv).maybe_send_for_user("u1")
    assert result.status == "skipped" and result.reason == "unseen_no_activity"


async def test_maybe_send_generates_content_when_new_activity():
    """New reflection since the last (seen) ping → fresh LLM content ping."""
    now = datetime.now(timezone.utc)
    db = AsyncMock()
    db.get_user_profile = AsyncMock(return_value=_profile())
    db.get_last_soul_ping = AsyncMock(
        return_value=_seen_ping(_iso(now - timedelta(hours=2)))
    )
    db.get_user_device_tokens = AsyncMock(
        return_value=[{"endpoint_arn": "arn:1", "is_active": True}]
    )
    db.save_soul_ping = AsyncMock(return_value=True)
    fresh = SimpleNamespace(
        summary="Working through stress.",
        key_themes=["stress"],
        open_threads=[],
        conversation_id="c1",
        last_message_at=_iso(now - timedelta(minutes=5)),  # newer than last ping
    )
    conv = AsyncMock()
    conv.get_recent_conversations = AsyncMock(return_value=[fresh])
    conv.get_conversation_history = AsyncMock(return_value=[])
    openai = AsyncMock()
    openai.send_with_overrides_async = AsyncMock(
        return_value='{"category":"emotional","title":"Hi","body":"You seem stressed."}'
    )
    sns = AsyncMock()
    sns.publish_to_endpoint_async = AsyncMock(return_value="m")

    result = await _build(db=db, openai=openai, conv=conv, sns=sns).maybe_send_for_user(
        "u1"
    )
    assert result.status == "sent"
    openai.send_with_overrides_async.assert_called_once()  # content path used the LLM


async def test_mark_read_passthrough():
    db = AsyncMock()
    db.mark_soul_ping_read = AsyncMock(return_value=True)
    assert await _build(db=db).mark_read("u1", "p1") is True
    db.mark_soul_ping_read.assert_awaited_once_with("u1", "p1")
