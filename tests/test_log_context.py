"""Correlation IDs in logs (request_id + user_id).

Verifies the record factory injects request_id/user_id onto every LogRecord,
that they default to "-", reflect the context vars, and that an HTTP request
binds a request id end-to-end (returned via X-Request-ID) without leaking
between requests.
"""

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.core.error_handlers import setup_error_handlers
from src.app.core.request_context import (
    get_request_id,
    get_user_id,
    install_log_record_factory,
    reset_request_id,
    set_request_id,
    set_user_id,
)


def test_factory_defaults_to_dash():
    install_log_record_factory()
    record = logging.getLogRecordFactory()("n", logging.INFO, "f", 1, "msg", None, None)
    # request_id/user_id are injected dynamically by the factory.
    assert getattr(record, "request_id") == "-"
    assert getattr(record, "user_id") == "-"


def test_factory_reflects_context():
    install_log_record_factory()
    rid = set_request_id("req-123")
    uid = set_user_id("user-abc")
    try:
        record = logging.getLogRecordFactory()(
            "n", logging.INFO, "f", 1, "msg", None, None
        )
        assert getattr(record, "request_id") == "req-123"
        assert getattr(record, "user_id") == "user-abc"
    finally:
        reset_request_id(rid)
        # user reset via fresh default
        set_user_id(None)


def test_install_is_idempotent():
    install_log_record_factory()
    first = logging.getLogRecordFactory()
    install_log_record_factory()
    assert logging.getLogRecordFactory() is first


def test_set_none_is_default():
    set_user_id(None)
    assert get_user_id() == "-"
    token = set_request_id("")
    assert get_request_id() == "-"
    reset_request_id(token)


def _app() -> FastAPI:
    app = FastAPI()
    setup_error_handlers(app)

    @app.get("/ping")
    async def ping():
        return {"request_id": get_request_id()}

    return app


def test_request_binds_and_returns_request_id():
    client = TestClient(_app())
    resp = client.get("/ping")
    assert resp.status_code == 200
    # The handler saw a bound (non-default) request id...
    body_id = resp.json()["request_id"]
    assert body_id != "-"
    # ...and it's echoed back in the response header.
    assert resp.headers["X-Request-ID"] == body_id


def test_inbound_request_id_is_honored():
    client = TestClient(_app())
    resp = client.get("/ping", headers={"X-Request-ID": "trace-xyz"})
    assert resp.headers["X-Request-ID"] == "trace-xyz"
    assert resp.json()["request_id"] == "trace-xyz"


def test_request_id_does_not_leak_after_request():
    client = TestClient(_app())
    client.get("/ping", headers={"X-Request-ID": "trace-xyz"})
    # After the request completes the context is reset to default.
    assert get_request_id() == "-"
