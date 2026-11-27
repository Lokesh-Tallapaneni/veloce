"""Tests for veloce.middleware.logging."""

from __future__ import annotations

import asyncio
import logging

import pytest

from tests.conftest import make_request
from veloce import Response, Veloce
from veloce._constants import HEADER_X_REQUEST_ID
from veloce.http.request import Request
from veloce.middleware.logging import LoggingMiddleware, RequestIDMiddleware


def _request(headers=None) -> Request:
    return make_request(
        method="GET",
        path="/",
        query_string="",
        headers=headers or {},
        body=b"",
    )


async def test_request_id_rejects_malformed_inbound():
    # A CR/LF in the inbound X-Request-ID would crash header emission when
    # reflected; the middleware must mint a fresh id instead.
    mw = RequestIDMiddleware()
    req = _request(headers={"x-request-id": "ok\r\nX-Evil: 1"})
    await mw.process_request(req)
    rid = req.state["request_id"]
    assert "\r" not in rid
    assert "\n" not in rid
    assert rid != "ok\r\nX-Evil: 1"


async def test_request_id_preserves_valid_inbound():
    mw = RequestIDMiddleware()
    req = _request(headers={"x-request-id": "abc-123"})
    await mw.process_request(req)
    assert req.state["request_id"] == "abc-123"


@pytest.fixture
def access_logger_state():
    """Snapshot and restore veloce.access logger state across the test."""
    logger = logging.getLogger("veloce.access")
    saved_level = logger.level
    saved_handlers = list(logger.handlers)
    saved_propagate = logger.propagate
    # Clear so each test starts from a known baseline.
    logger.handlers = []
    logger.setLevel(logging.NOTSET)
    try:
        yield logger
    finally:
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def test_default_bootstrap_sets_info_on_untouched_logger(access_logger_state):
    """With no pre-configuration, LoggingMiddleware sets INFO and adds a handler."""
    logger = access_logger_state
    assert logger.level == logging.NOTSET
    assert logger.handlers == []

    LoggingMiddleware()

    assert logger.level == logging.INFO
    assert len(logger.handlers) == 1


def test_preconfigured_level_is_respected(access_logger_state):
    """A pre-set WARNING level on veloce.access must survive middleware init."""
    logger = access_logger_state
    pre_handler = logging.NullHandler()
    logger.addHandler(pre_handler)
    logger.setLevel(logging.WARNING)

    LoggingMiddleware()

    assert logger.level == logging.WARNING
    # The pre-existing handler must still be the only handler — no
    # bootstrap StreamHandler appended.
    assert logger.handlers == [pre_handler]


def test_explicit_logger_argument_untouched(access_logger_state):
    """Passing logger= bypasses all bootstrap logic."""
    custom = logging.getLogger("test_explicit_logger_argument_untouched")
    custom.handlers = []
    custom.setLevel(logging.ERROR)

    mw = LoggingMiddleware(logger=custom)

    assert mw.logger is custom
    assert custom.level == logging.ERROR
    assert custom.handlers == []


def test_logging_respects_handler_only_or_level_only_config(access_logger_state):
    """Handlers and level are independent; bootstrapping must check each separately.

    R1 #23: the prior fix gated both `addHandler` and `setLevel(INFO)`
    on `not self.logger.handlers`, so a user who pre-installed a
    defensive `NullHandler` without setting a level would end up at
    NOTSET (inherits root → typically WARNING) and silently lose
    access logs.
    """
    # Case 1: user added a NullHandler but never set a level → we
    # MUST still bootstrap the level to INFO without adding a second
    # handler.
    logger = access_logger_state
    pre_handler = logging.NullHandler()
    logger.addHandler(pre_handler)
    assert logger.level == logging.NOTSET

    LoggingMiddleware()

    assert logger.level == logging.INFO  # level was bootstrapped
    assert logger.handlers == [pre_handler]  # no extra handler appended

    # Case 2: user set a level but installed no handler → we MUST
    # add our StreamHandler without overriding the level.
    logger.handlers = []
    logger.setLevel(logging.DEBUG)

    LoggingMiddleware()

    assert logger.level == logging.DEBUG  # pre-set level preserved
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], logging.StreamHandler)


# ── Log-injection / forging (CWE-117) ───────────────────────────────


async def test_control_chars_in_path_are_escaped(access_logger_state):
    # On the ASGI path the server percent-decodes the URL into request.path, so
    # a %0a/%0d arrives as a real newline/CR. The access line must escape those
    # control characters so an attacker cannot forge or split log records.

    logger = access_logger_state
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger.addHandler(_Capture())
    logger.setLevel(logging.INFO)

    mw = LoggingMiddleware(logger=logger)
    request = Request(
        method="GET",
        path="/a\r\n2026-01-01 00:00:00 - INFO forged-admin-action",
        query_string="",
        headers={},
        body=b"",
    )
    await mw.process_request(request)
    await mw.process_response(request, Response())

    assert len(records) == 1
    message = records[0].getMessage()
    # No raw line breaks survive into the rendered access line.
    assert "\n" not in message
    assert "\r" not in message
    # The control characters are present in escaped form instead.
    assert "\\x0d\\x0a" in message
    assert "forged-admin-action" in message


async def test_clean_path_is_logged_unchanged(access_logger_state):
    # A normal method / path contains no control characters, so the escape is a
    # no-op and the access line reads exactly as before.

    logger = access_logger_state
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger.addHandler(_Capture())
    logger.setLevel(logging.INFO)

    mw = LoggingMiddleware(logger=logger)
    request = Request(method="GET", path="/users/42", query_string="q=1", headers={}, body=b"")
    await mw.process_request(request)
    await mw.process_response(request, Response())

    message = records[0].getMessage()
    assert message.startswith("GET /users/42 200")
    assert "\\x" not in message


# ── RequestIDMiddleware ─────────────────────────────────────────────


async def test_request_id_uses_incoming_header():
    mw = RequestIDMiddleware()
    request = _request({HEADER_X_REQUEST_ID: "abc-123"})
    await mw.process_request(request)
    assert request.state["request_id"] == "abc-123"


async def test_request_id_generated_when_header_missing():
    mw = RequestIDMiddleware()
    request = _request()
    await mw.process_request(request)
    assert request.state["request_id"]


async def test_request_id_generated_when_header_empty():
    # An empty incoming X-Request-ID is unusable; a fresh ID is generated
    # rather than propagating the empty string.
    mw = RequestIDMiddleware()
    request = _request({HEADER_X_REQUEST_ID: ""})
    await mw.process_request(request)
    assert request.state["request_id"]


async def test_logging_middleware_does_not_leak_on_handler_exception(caplog):
    """A handler that raises must not leave state behind on the middleware.

    The start time lives on `request._state`, whose lifetime ends with the
    request, so even on a raise nothing accumulates at the middleware level.

    This never raised - it called `process_request` and stopped - so the path
    the name promises went untested, and `process_response`, where the key is
    popped, was never reached. `assert not hasattr(mw, "_request_times")` named
    only the attribute the original bug used, so a reimplementation holding a
    per-request dict under any other name passed unchanged. It now drives three
    failing requests end to end and compares the middleware's whole instance
    dict, which no rename gets past.
    """
    mw = LoggingMiddleware()
    app = Veloce(openapi_url=None)
    app.add_middleware(mw)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("handler failed")

    with caplog.at_level(logging.INFO):
        before = dict(vars(mw))
        for _ in range(3):
            response = await app.handle_request(make_request(path="/boom"))
            assert response.status_code == 500

        assert vars(mw) == before


async def test_logging_middleware_durations_are_per_request():
    """Two concurrent requests must each see their own start time —
    no id() collision via a shared dict."""
    mw = LoggingMiddleware()
    r1 = Request(method="GET", path="/a", query_string="", headers={}, body=b"")
    r2 = Request(method="GET", path="/b", query_string="", headers={}, body=b"")
    await mw.process_request(r1)
    await asyncio.sleep(0.01)
    await mw.process_request(r2)
    # The property: each request carries its *own* entry. A `>=` between the two
    # timestamps - what this used to close on - is clock monotonicity, which
    # holds under every implementation including the shared-dict collision the
    # test is named for: both would read the same value, and `>=` allows that.
    assert r1.state is not r2.state
    assert "__veloce_logging_start" in r1.state
    assert "__veloce_logging_start" in r2.state
    s1 = r1.state["__veloce_logging_start"]
    s2 = r2.state["__veloce_logging_start"]
    # Ordering still holds, with `>=` rather than `>` because a coarse clock
    # (Windows' wall clock is ~15 ms) can return the same value twice 10 ms
    # apart.
    assert s2 >= s1

    # `process_response` clears its own request's entry and must not reach the
    # other's - which a shared dict keyed by `id(request)` could not promise.
    await mw.process_response(r1, Response(status_code=200))
    assert "__veloce_logging_start" not in r1.state
    assert r2.state["__veloce_logging_start"] == s2
    await mw.process_response(r2, Response(status_code=200))
    assert "__veloce_logging_start" not in r2.state


def test_logging_middleware_sets_level_when_handler_preconfigured() -> None:
    logger = logging.getLogger("veloce.access")
    # Snapshot state so we can restore after the test.
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    try:
        logger.handlers = [logging.NullHandler()]
        logger.setLevel(logging.NOTSET)
        assert logger.level == logging.NOTSET

        LoggingMiddleware()

        assert logger.level == logging.INFO
        # The pre-existing NullHandler must not have been duplicated.
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0], logging.NullHandler)
    finally:
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)
