"""FastAPI decorator for idempotent write endpoints.

Usage
-----

    from ..core.idempotency import idempotent

    @router.post("/echoes")
    @idempotent(route_id="create_echo")
    async def create_echo(request: ..., current_user=...):
        ...

The decorator looks for ``Idempotency-Key`` on the incoming request.
If present and a cached response exists for ``(user_id, route_id, key)``,
the cached payload is returned verbatim — the route handler is never
invoked. Otherwise the handler runs normally and its response is
recorded under the key with a 24 h TTL.

Why a decorator over middleware
-------------------------------
The middleware would have to identify which routes participate in
idempotency, parse the user_id from a downstream auth dependency, and
buffer the entire response body. A decorator sees the resolved
``current_user`` cleanly and only the routes that explicitly opt in
pay the cache round-trip.

Contract details
----------------
- If the header is missing, the wrapper is a no-op (handler runs as-is).
  Idempotency is opt-in per-call from the client, not server-enforced.
- If the handler raises, nothing is cached — failures are not idempotent
  by design. The client SHOULD retry a failure with the same key, and
  the server WILL run the handler again (which is the correct behavior:
  the first call's side effects never completed).
- Only ``2xx`` responses are cached. Caching a 4xx would mask a genuine
  validation failure on retry.
- Keys are length-bounded (max 200 chars) to prevent abuse via giant
  client keys.
"""

import functools
import inspect
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import HTTPException, Request

from ..services.idempotency_service import get_idempotency_service

logger = logging.getLogger(__name__)

# Cap the client key length to keep DDB items small. Long keys are not
# functional — clients should send a UUID, which is 36 chars.
MAX_CLIENT_KEY_LEN = 200


def _extract_client_key(request: Optional[Request]) -> Optional[str]:
    """Pull the Idempotency-Key header off the request. Header names are
    case-insensitive per RFC; FastAPI's mapping handles that. Returns
    None if the header is missing or empty.
    """
    if request is None:
        return None
    key = request.headers.get("Idempotency-Key") or request.headers.get(
        "idempotency-key"
    )
    if not key:
        return None
    key = key.strip()
    if not key:
        return None
    if len(key) > MAX_CLIENT_KEY_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Idempotency-Key exceeds {MAX_CLIENT_KEY_LEN} chars",
        )
    return key


def _extract_user_id(kwargs: Dict[str, Any]) -> Optional[str]:
    """Pull user_id out of the resolved `current_user` dependency. The
    auth dependency in this codebase puts the Cognito sub at
    ``current_user['id']``.
    """
    cu = kwargs.get("current_user")
    if not isinstance(cu, dict):
        return None
    user_id = cu.get("id")
    return user_id if isinstance(user_id, str) and user_id else None


def idempotent(route_id: str) -> Callable[..., Callable[..., Awaitable[Any]]]:
    """Decorator factory; bind ``route_id`` per route.

    The decorated handler must accept a ``request: Request`` parameter
    (FastAPI's injection covers it automatically) and a ``current_user``
    keyword argument from the auth dependency.
    """

    def decorator(
        handler: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[Any]]:
        # Verify the decorated function exposes the Request parameter so
        # FastAPI's dependency injection wires it for us.
        sig = inspect.signature(handler)
        has_request_param = "request" in sig.parameters

        @functools.wraps(handler)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            request: Optional[Request] = (
                kwargs.get("request") if has_request_param else None
            )
            client_key = _extract_client_key(request)
            user_id = _extract_user_id(kwargs)

            # If either is missing, fall through to the handler — the
            # client didn't opt into idempotency or auth isn't ready.
            if not client_key or not user_id:
                return await handler(*args, **kwargs)

            service = get_idempotency_service()

            # Cache lookup.
            cached = await service.get_cached(
                user_id=user_id, route=route_id, client_key=client_key
            )
            if cached is not None:
                logger.info(
                    f"Idempotency HIT route={route_id} user={user_id} "
                    f"key={client_key} status={cached['status_code']}"
                )
                return cached["body"]

            # Cache miss — run the handler and capture its response.
            response = await handler(*args, **kwargs)

            # We only cache 2xx-ish responses. The handlers in this
            # codebase return dicts on success and raise HTTPException
            # on failure (which never reaches this point), so a
            # successful return here is effectively a 2xx.
            if isinstance(response, dict):
                await service.cache(
                    user_id=user_id,
                    route=route_id,
                    client_key=client_key,
                    status_code=200,
                    body=response,
                )
            return response

        return wrapper

    return decorator
