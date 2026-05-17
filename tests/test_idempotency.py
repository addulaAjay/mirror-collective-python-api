"""Tests for the idempotency cache.

Covers:
- IdempotencyService.get_cached: miss / hit / expired / corrupt-body /
  DDB failure.
- IdempotencyService.cache: write succeeds, race resolved (Conditional
  Check), DDB failure absorbed.
- @idempotent decorator: no header → handler runs; header + cache miss
  → handler runs and result is stored; header + cache hit → handler
  skipped; oversize key → 400; non-dict response not cached.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.app.core.idempotency import idempotent
from src.app.services.idempotency_service import (
    IDEMPOTENCY_TTL_SECONDS,
    IdempotencyService,
    build_namespace,
)


def _ddb_resource_mock(get_item_return=None, put_item_side_effect=None):
    table = AsyncMock()
    if get_item_return is not None:
        table.get_item = AsyncMock(return_value=get_item_return)
    else:
        table.get_item = AsyncMock(return_value={})
    if put_item_side_effect is not None:
        table.put_item = AsyncMock(side_effect=put_item_side_effect)
    else:
        table.put_item = AsyncMock()
    resource = AsyncMock()
    resource.Table = AsyncMock(return_value=table)
    return resource, table


# ---------------------------------------------------------------------
# build_namespace
# ---------------------------------------------------------------------


def test_build_namespace_includes_user_route_and_key():
    ns = build_namespace("u-1", "create_echo", "abc-123")
    assert ns == "u-1|create_echo|abc-123"


# ---------------------------------------------------------------------
# IdempotencyService.get_cached
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cached_miss_returns_none():
    svc = IdempotencyService()
    resource, _ = _ddb_resource_mock(get_item_return={})
    with patch.object(svc, "_get_resource", new=AsyncMock(return_value=resource)):
        result = await svc.get_cached("u", "create_echo", "k")
    assert result is None


@pytest.mark.asyncio
async def test_get_cached_hit_returns_status_and_body():
    svc = IdempotencyService()
    future = int(time.time()) + 1000
    resource, _ = _ddb_resource_mock(
        get_item_return={
            "Item": {
                "key_namespace": "u|create_echo|k",
                "status_code": 200,
                "response_body": '{"echo_id":"e-1"}',
                "expires_at": future,
            }
        }
    )
    with patch.object(svc, "_get_resource", new=AsyncMock(return_value=resource)):
        result = await svc.get_cached("u", "create_echo", "k")
    assert result == {"status_code": 200, "body": {"echo_id": "e-1"}}


@pytest.mark.asyncio
async def test_get_cached_treats_expired_row_as_miss():
    """DDB's TTL sweep is async, so an item with expires_at in the past
    can still be returned by get_item. The service must filter those.
    """
    svc = IdempotencyService()
    past = int(time.time()) - 10
    resource, _ = _ddb_resource_mock(
        get_item_return={
            "Item": {
                "key_namespace": "u|create_echo|k",
                "status_code": 200,
                "response_body": '{"x":1}',
                "expires_at": past,
            }
        }
    )
    with patch.object(svc, "_get_resource", new=AsyncMock(return_value=resource)):
        result = await svc.get_cached("u", "create_echo", "k")
    assert result is None


@pytest.mark.asyncio
async def test_get_cached_corrupt_body_returns_none():
    svc = IdempotencyService()
    future = int(time.time()) + 1000
    resource, _ = _ddb_resource_mock(
        get_item_return={
            "Item": {
                "key_namespace": "u|create_echo|k",
                "status_code": 200,
                "response_body": "this-is-not-json{{",
                "expires_at": future,
            }
        }
    )
    with patch.object(svc, "_get_resource", new=AsyncMock(return_value=resource)):
        result = await svc.get_cached("u", "create_echo", "k")
    assert result is None


@pytest.mark.asyncio
async def test_get_cached_swallows_ddb_failure():
    """Cache failures must NOT propagate — idempotency is opportunistic."""
    svc = IdempotencyService()
    table = AsyncMock()
    table.get_item = AsyncMock(
        side_effect=ClientError({"Error": {"Code": "InternalServerError"}}, "GetItem")
    )
    resource = AsyncMock()
    resource.Table = AsyncMock(return_value=table)
    with patch.object(svc, "_get_resource", new=AsyncMock(return_value=resource)):
        result = await svc.get_cached("u", "create_echo", "k")
    assert result is None


# ---------------------------------------------------------------------
# IdempotencyService.cache
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_writes_with_24h_ttl():
    svc = IdempotencyService()
    resource, table = _ddb_resource_mock()
    with patch.object(svc, "_get_resource", new=AsyncMock(return_value=resource)):
        await svc.cache(
            user_id="u",
            route="create_echo",
            client_key="k",
            status_code=200,
            body={"echo_id": "e-1"},
        )
    table.put_item.assert_awaited_once()
    args, _ = table.put_item.call_args.args, table.put_item.call_args.kwargs
    item = table.put_item.call_args.kwargs["Item"]
    assert item["key_namespace"] == "u|create_echo|k"
    assert item["status_code"] == 200
    assert item["response_body"] == '{"echo_id": "e-1"}'
    # expires_at is roughly now + 24 h.
    assert item["expires_at"] - item["created_at"] == IDEMPOTENCY_TTL_SECONDS
    # ConditionExpression enforces first-writer-wins.
    assert (
        "attribute_not_exists" in table.put_item.call_args.kwargs["ConditionExpression"]
    )


@pytest.mark.asyncio
async def test_cache_swallows_conditional_check_failure():
    """Two requests race with the same key — second write's
    ConditionalCheckFailed is the expected outcome, not an error.
    """
    svc = IdempotencyService()
    resource, _ = _ddb_resource_mock(
        put_item_side_effect=ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
        )
    )
    with patch.object(svc, "_get_resource", new=AsyncMock(return_value=resource)):
        # Should NOT raise.
        await svc.cache("u", "create_echo", "k", 200, {"x": 1})


@pytest.mark.asyncio
async def test_cache_swallows_unexpected_ddb_error():
    svc = IdempotencyService()
    resource, _ = _ddb_resource_mock(
        put_item_side_effect=ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "PutItem",
        )
    )
    with patch.object(svc, "_get_resource", new=AsyncMock(return_value=resource)):
        # Should NOT raise — caching is opportunistic.
        await svc.cache("u", "create_echo", "k", 200, {"x": 1})


# ---------------------------------------------------------------------
# @idempotent decorator
# ---------------------------------------------------------------------


class _FakeRequest:
    """Minimal stand-in for starlette.Request — only headers are read."""

    def __init__(self, headers=None):
        self.headers = headers or {}


@pytest.mark.asyncio
async def test_decorator_no_header_runs_handler_normally():
    calls = []

    @idempotent(route_id="x")
    async def handler(*, request, current_user):
        calls.append("ran")
        return {"ok": True}

    result = await handler(
        request=_FakeRequest(headers={}),
        current_user={"id": "u-1"},
    )
    assert calls == ["ran"]
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_decorator_oversize_key_returns_400():
    from fastapi import HTTPException

    @idempotent(route_id="x")
    async def handler(*, request, current_user):
        return {"ok": True}

    long_key = "a" * 201
    with pytest.raises(HTTPException) as exc_info:
        await handler(
            request=_FakeRequest(headers={"Idempotency-Key": long_key}),
            current_user={"id": "u-1"},
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_decorator_cache_hit_skips_handler_and_returns_cached_body():
    """Cache hit: handler must NOT run."""
    calls = []

    @idempotent(route_id="x")
    async def handler(*, request, current_user):
        calls.append("ran")
        return {"ok": True, "fresh": True}

    fake_service = MagicMock()
    fake_service.get_cached = AsyncMock(
        return_value={"status_code": 200, "body": {"ok": True, "cached": True}}
    )
    fake_service.cache = AsyncMock()

    with patch(
        "src.app.core.idempotency.get_idempotency_service",
        return_value=fake_service,
    ):
        result = await handler(
            request=_FakeRequest(headers={"Idempotency-Key": "k1"}),
            current_user={"id": "u-1"},
        )

    assert calls == [], "handler must not run on cache hit"
    assert result == {"ok": True, "cached": True}
    fake_service.cache.assert_not_called()


@pytest.mark.asyncio
async def test_decorator_cache_miss_runs_handler_and_stores_response():
    calls = []

    @idempotent(route_id="x")
    async def handler(*, request, current_user):
        calls.append("ran")
        return {"ok": True}

    fake_service = MagicMock()
    fake_service.get_cached = AsyncMock(return_value=None)
    fake_service.cache = AsyncMock()

    with patch(
        "src.app.core.idempotency.get_idempotency_service",
        return_value=fake_service,
    ):
        result = await handler(
            request=_FakeRequest(headers={"Idempotency-Key": "k1"}),
            current_user={"id": "u-1"},
        )

    assert calls == ["ran"]
    assert result == {"ok": True}
    fake_service.cache.assert_awaited_once()
    cache_kwargs = fake_service.cache.call_args.kwargs
    assert cache_kwargs["user_id"] == "u-1"
    assert cache_kwargs["route"] == "x"
    assert cache_kwargs["client_key"] == "k1"
    assert cache_kwargs["body"] == {"ok": True}


@pytest.mark.asyncio
async def test_decorator_no_user_in_kwargs_passes_through():
    """If the handler isn't using the standard auth dependency (e.g.
    public endpoint with the decorator accidentally applied), the
    decorator must not crash — just pass through.
    """
    calls = []

    @idempotent(route_id="x")
    async def handler(*, request, current_user):
        calls.append("ran")
        return {"ok": True}

    # current_user empty / no id.
    result = await handler(
        request=_FakeRequest(headers={"Idempotency-Key": "k1"}),
        current_user={},
    )
    assert calls == ["ran"]
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_decorator_does_not_cache_non_dict_responses():
    """Handler returning a non-dict (e.g. a Response object) should be
    passed through but NOT cached — we don't have a way to serialize
    those into the cache row.
    """

    @idempotent(route_id="x")
    async def handler(*, request, current_user):
        return "not-a-dict"

    fake_service = MagicMock()
    fake_service.get_cached = AsyncMock(return_value=None)
    fake_service.cache = AsyncMock()

    with patch(
        "src.app.core.idempotency.get_idempotency_service",
        return_value=fake_service,
    ):
        result = await handler(
            request=_FakeRequest(headers={"Idempotency-Key": "k1"}),
            current_user={"id": "u-1"},
        )

    assert result == "not-a-dict"
    fake_service.cache.assert_not_called()
