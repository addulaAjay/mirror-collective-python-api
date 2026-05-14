"""Tests for the long-lived aioboto3 resource + quiz-questions cache in
``DynamoDBService``.

These tests exercise the perf wins from Wave 1B-E3:

A) ``_get_resource`` returns the same aioboto3 resource across calls — the
   underlying session.resource() context manager is entered exactly once.

B) ``get_quiz_questions`` caches scan results in-process with a TTL, only
   re-scans after the TTL elapses, and collapses concurrent callers onto a
   single scan via an asyncio lock.

The global conftest replaces ``DynamoDBService`` with a Mock for the rest of
the test suite. We stop that patcher locally so we can construct and exercise
the real class, then restart it on teardown so other tests are unaffected.
"""

from __future__ import annotations

import asyncio
import importlib
from contextlib import asynccontextmanager
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests import conftest as _conftest


@pytest.fixture
def dynamodb_service_cls():
    """Yield the real ``DynamoDBService`` class (un-patched).

    The global ``dynamodb_service_patcher`` in ``tests/conftest.py`` replaces
    the class symbol with a Mock at import time. We stop it for the duration
    of this test, reload the module to restore the real class, and restart
    the patcher on teardown so the rest of the suite still sees the Mock.
    """
    _conftest.dynamodb_service_patcher.stop()
    try:
        import src.app.services.dynamodb_service as ddb_module

        importlib.reload(ddb_module)
        yield ddb_module.DynamoDBService, ddb_module
    finally:
        # Restart the patcher so the rest of the suite sees the Mock again.
        _conftest.mock_dynamodb_service = _conftest.dynamodb_service_patcher.start()


def _make_fake_resource_context():
    """Build a fake async-context-manager mimicking ``session.resource(...)``.

    Returns a tuple of ``(context_factory, enter_count_ref, exit_count_ref,
    resource_mock)`` so tests can assert how many times the CM was entered.
    """
    enter_count = {"n": 0}
    exit_count = {"n": 0}
    resource_mock = MagicMock(name="DynamoDBResource")

    @asynccontextmanager
    async def _cm():
        enter_count["n"] += 1
        try:
            yield resource_mock
        finally:
            exit_count["n"] += 1

    def _factory(*_args, **_kwargs):
        return _cm()

    return _factory, enter_count, exit_count, resource_mock


# ---------------------------------------------------------------------------
# A) Long-lived resource reuse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resource_is_reused_across_calls(dynamodb_service_cls):
    """`_get_resource` enters the underlying session.resource() exactly once.

    This is the whole point of the long-lived resource refactor: instead of
    paying TLS/credential/endpoint costs every call, we pay them once.
    """
    DynamoDBService, _ddb_module = dynamodb_service_cls

    factory, enter_count, exit_count, resource_mock = _make_fake_resource_context()

    service = DynamoDBService()
    with patch.object(service.session, "resource", side_effect=factory):
        r1 = await service._get_resource()
        r2 = await service._get_resource()
        r3 = await service._get_resource()

    assert r1 is resource_mock
    assert r2 is resource_mock
    assert r3 is resource_mock
    assert enter_count["n"] == 1, "session.resource() must only be entered once"
    assert exit_count["n"] == 0, "resource must stay open until close()"

    # close() unwinds the AsyncExitStack
    await service.close()
    assert exit_count["n"] == 1
    # And close() is idempotent
    await service.close()
    assert exit_count["n"] == 1


@pytest.mark.asyncio
async def test_resource_init_is_concurrency_safe(dynamodb_service_cls):
    """Concurrent first-time callers collapse onto a single resource init.

    Without the double-checked lock in `_get_resource`, a burst of concurrent
    calls during cold start would each construct their own aioboto3 resource.
    """
    DynamoDBService, _ddb_module = dynamodb_service_cls

    factory, enter_count, _exit_count, resource_mock = _make_fake_resource_context()

    # Slow down the first enter so concurrent callers pile up on the lock.
    slow_factory_calls = {"n": 0}

    @asynccontextmanager
    async def _slow_cm():
        slow_factory_calls["n"] += 1
        await asyncio.sleep(0.01)
        async with factory() as r:
            yield r

    service = DynamoDBService()
    with patch.object(
        service.session, "resource", side_effect=lambda *a, **k: _slow_cm()
    ):
        results = await asyncio.gather(*(service._get_resource() for _ in range(10)))

    assert all(r is resource_mock for r in results)
    assert enter_count["n"] == 1, "Only one resource should be constructed"
    assert slow_factory_calls["n"] == 1

    await service.close()


# ---------------------------------------------------------------------------
# B) Quiz-questions cache
# ---------------------------------------------------------------------------


def _patch_quiz_cache_reset(ddb_module):
    """Clear module-level quiz cache between tests."""
    ddb_module._QUIZ_CACHE = None


def _install_quiz_scan_stub(
    service: Any, items: List[Dict[str, Any]]
) -> Dict[str, int]:
    """Wire up a fake DDB resource so scan() returns `items` and counts calls."""
    scan_calls = {"n": 0}

    async def _scan(*_a, **_kw):
        scan_calls["n"] += 1
        return {"Items": list(items)}

    fake_table = MagicMock()
    fake_table.scan = AsyncMock(side_effect=_scan)

    fake_resource = MagicMock()
    fake_resource.Table = AsyncMock(return_value=fake_table)

    async def _get_resource():
        return fake_resource

    service._get_resource = _get_resource  # type: ignore[assignment]
    return scan_calls


@pytest.mark.asyncio
async def test_quiz_questions_cache_hit_within_ttl(dynamodb_service_cls):
    """Second call within the TTL window is served from cache (no scan)."""
    DynamoDBService, ddb_module = dynamodb_service_cls
    _patch_quiz_cache_reset(ddb_module)

    service = DynamoDBService()
    scan_calls = _install_quiz_scan_stub(service, [{"id": "q1"}, {"id": "q2"}])

    first = await service.get_quiz_questions()
    second = await service.get_quiz_questions()

    assert first == [{"id": "q1"}, {"id": "q2"}]
    assert second == first
    assert scan_calls["n"] == 1, "Cache hit must not trigger a second scan"


@pytest.mark.asyncio
async def test_quiz_questions_cache_expiry(dynamodb_service_cls, monkeypatch):
    """After the TTL elapses, the cache is refreshed via a new scan."""
    DynamoDBService, ddb_module = dynamodb_service_cls
    _patch_quiz_cache_reset(ddb_module)
    monkeypatch.setattr(ddb_module, "_QUIZ_CACHE_TTL", 60)

    fake_now = {"t": 1000.0}

    def _monotonic():
        return fake_now["t"]

    monkeypatch.setattr(ddb_module.time, "monotonic", _monotonic)

    service = DynamoDBService()
    scan_calls = _install_quiz_scan_stub(service, [{"id": "q1"}])

    await service.get_quiz_questions()  # populates cache
    assert scan_calls["n"] == 1

    # Within TTL — still cached
    fake_now["t"] = 1030.0
    await service.get_quiz_questions()
    assert scan_calls["n"] == 1

    # Past TTL — re-scans
    fake_now["t"] = 1061.0
    await service.get_quiz_questions()
    assert scan_calls["n"] == 2


@pytest.mark.asyncio
async def test_quiz_questions_cache_thread_safety(dynamodb_service_cls):
    """Concurrent first-time callers collapse onto a single scan."""
    DynamoDBService, ddb_module = dynamodb_service_cls
    _patch_quiz_cache_reset(ddb_module)

    service = DynamoDBService()

    scan_calls = {"n": 0}

    async def _slow_scan(*_a, **_kw):
        scan_calls["n"] += 1
        # Hold the lock long enough for other coroutines to pile up.
        await asyncio.sleep(0.02)
        return {"Items": [{"id": "q1"}]}

    fake_table = MagicMock()
    fake_table.scan = AsyncMock(side_effect=_slow_scan)
    fake_resource = MagicMock()
    fake_resource.Table = AsyncMock(return_value=fake_table)

    async def _get_resource():
        return fake_resource

    service._get_resource = _get_resource  # type: ignore[assignment]

    results = await asyncio.gather(*(service.get_quiz_questions() for _ in range(10)))

    assert all(r == [{"id": "q1"}] for r in results)
    assert (
        scan_calls["n"] == 1
    ), "10 concurrent get_quiz_questions calls must collapse onto a single scan"


@pytest.mark.asyncio
async def test_quiz_questions_failure_is_not_cached(dynamodb_service_cls):
    """A transient DDB failure must not poison the cache for 5 minutes."""
    DynamoDBService, ddb_module = dynamodb_service_cls
    _patch_quiz_cache_reset(ddb_module)

    service = DynamoDBService()

    call_count = {"n": 0}

    async def _flaky_scan(*_a, **_kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient")
        return {"Items": [{"id": "q1"}]}

    fake_table = MagicMock()
    fake_table.scan = AsyncMock(side_effect=_flaky_scan)
    fake_resource = MagicMock()
    fake_resource.Table = AsyncMock(return_value=fake_table)

    async def _get_resource():
        return fake_resource

    service._get_resource = _get_resource  # type: ignore[assignment]

    first = await service.get_quiz_questions()
    assert first == []  # error path returns empty list

    second = await service.get_quiz_questions()
    assert second == [{"id": "q1"}]
    assert call_count["n"] == 2
