"""In-memory stand-in for IdempotencyService used in route tests.

Mirrors the real service's ``get_cached`` / ``cache`` contract (namespace on
``user_id|route|client_key``; first-writer-wins) without touching DynamoDB, so
dedup behavior can be asserted end-to-end through the TestClient.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from src.app.services.idempotency_service import build_namespace


class FakeIdempotencyService:
    """Dict-backed cache with the same surface as IdempotencyService."""

    def __init__(self) -> None:
        self.store: Dict[str, Dict[str, Any]] = {}

    async def get_cached(
        self, user_id: str, route: str, client_key: str
    ) -> Optional[Dict[str, Any]]:
        return self.store.get(build_namespace(user_id, route, client_key))

    async def cache(
        self,
        user_id: str,
        route: str,
        client_key: str,
        status_code: int,
        body: Dict[str, Any],
    ) -> None:
        ns = build_namespace(user_id, route, client_key)
        # First-writer-wins, matching the DDB ConditionExpression.
        self.store.setdefault(ns, {"status_code": status_code, "body": body})
