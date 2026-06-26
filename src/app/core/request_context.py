"""Request-scoped logging context (correlation IDs).

Holds a ``request_id`` (per HTTP request) and ``user_id`` (the authenticated
Cognito sub) in context variables, and installs a ``logging`` record factory so
*every* log line carries them. This makes a single request greppable end-to-end
and lets you follow one user across requests when they report an issue.

- ``request_id`` is set by the request-id middleware (honoring an inbound
  ``X-Request-ID`` for cross-service tracing) and returned in the response.
- ``user_id`` is set by the auth dependency once the token is decoded.

Both default to ``"-"`` so unauthenticated/background log lines are still valid.
"""

import contextvars
import logging
from typing import Optional

DEFAULT = "-"

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=DEFAULT
)
_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "user_id", default=DEFAULT
)


def set_request_id(value: Optional[str]) -> contextvars.Token:
    return _request_id.set(value or DEFAULT)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id.reset(token)


def get_request_id() -> str:
    return _request_id.get()


def set_user_id(value: Optional[str]) -> contextvars.Token:
    return _user_id.set(value or DEFAULT)


def reset_user_id(token: contextvars.Token) -> None:
    _user_id.reset(token)


def get_user_id() -> str:
    return _user_id.get()


def install_log_record_factory() -> None:
    """Wrap the active LogRecord factory so every record carries the context.

    Idempotent: re-installing won't stack wrappers. Using a record factory
    (rather than per-handler filters) guarantees the ``request_id``/``user_id``
    attributes exist on every record, so formatters that reference them never
    raise.
    """
    existing = logging.getLogRecordFactory()
    if getattr(existing, "_mc_context_factory", False):
        return

    def factory(*args, **kwargs):
        record = existing(*args, **kwargs)
        record.request_id = _request_id.get()
        record.user_id = _user_id.get()
        return record

    factory._mc_context_factory = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)
