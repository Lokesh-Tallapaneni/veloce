"""Shared test fixtures for Veloce test suite."""

import os

import pytest
from hypothesis import settings

from veloce import Request, Veloce

# Hypothesis profiles for the parser fuzz suite. The default keeps the
# per-example count modest so the fuzz tests run inside the normal `pytest`
# suite without slowing it down; the `ci` profile (selected by the CI fuzz leg
# via HYPOTHESIS_PROFILE=ci) explores more examples to catch parser
# regressions. A generous deadline avoids flaky timeouts under CPU contention.
settings.register_profile("default", deadline=None)
settings.register_profile("ci", max_examples=400, deadline=None)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture(autouse=True)
def _clear_graceful_drain_latch():
    """Clear the process-wide shutdown latch between tests.

    `HttpProtocol.start_graceful_drain()` sets a module global that makes every
    subsequently-admitted connection quiesce after one request. That is right in
    production, where shutdown is terminal, but a test that drives a server to
    shutdown would otherwise leave every later keep-alive and pipelining test
    serving a single request and failing. Clearing it is one global assignment.
    """
    yield
    from veloce.serving.protocol import HttpProtocol

    HttpProtocol.reset_graceful_drain()


@pytest.fixture(autouse=True)
def _isolate_custom_converters():
    """Restore the process-global converter registry between tests.

    `register_converter` writes into `routing.converters._CUSTOM`, which has no
    teardown and no `unregister_converter`. Every test that registered one
    leaked it into every later test, and the suite compensated by hand-numbering
    names (`slug`, `slug2`, `slug3`) so registrations would not collide - a
    workaround that has to be remembered by whoever adds the next one, and that
    silently stops working when two modules pick the same number.

    Snapshot and restore is one dict copy per test and removes the need for any
    of that.
    """
    from veloce.routing import converters

    saved = dict(converters._CUSTOM)
    yield
    converters._CUSTOM.clear()
    converters._CUSTOM.update(saved)


@pytest.fixture(autouse=True)
def _short_close_handshake_timeout(monkeypatch):
    """Shorten the WebSocket close handshake for the suite.

    `close()` on a server-initiated close waits for the peer's reply close frame
    (RFC 6455 Sec. 7.1.1) before dropping the transport, bounded by
    `CLOSE_HANDSHAKE_TIMEOUT = 5.0`. A test driving a raw socket through a fake
    transport has no peer to reply, so every such `close()` blocked the full five
    seconds: **25 tests, 125 seconds** - 93% of the websocket suite's runtime and
    over half the whole suite's, spent waiting for a reply that was never coming.

    No test asserted on the timeout's duration, so nothing is lost by shortening
    it here - and what the timeout actually does is now covered properly, and
    deterministically, in `tests/test_websocket_close_handshake.py`.
    """
    from veloce.websocket import WebSocket

    monkeypatch.setattr(WebSocket, "CLOSE_HANDSHAKE_TIMEOUT", 0.05)


@pytest.fixture
def app():
    """Fresh Veloce app with OpenAPI disabled for speed."""
    return Veloce(debug=True, openapi_url=None)


@pytest.fixture
def app_with_docs():
    """Veloce app with OpenAPI enabled."""
    return Veloce(debug=True)


def make_request(
    method: str = "GET",
    path: str = "/",
    headers: dict | None = None,
    body: bytes = b"",
    query_string: str = "",
    **extra,
) -> Request:
    """Build a `Request` for a test.

    The one place the suite constructs a `Request`. Dozens of modules used to
    re-derive this as a private `_req` / `_request` factory - 71 of them
    returning exactly this call with these five arguments, under mutually
    incompatible signatures - so a change to the constructor meant editing all of
    them.

    `**extra` forwards the less common constructor arguments (`app`, `scope`,
    `transport`) that a handful of modules need, so those do not have to fall
    back to building a `Request` by hand.
    """
    return Request(
        method=method,
        path=path,
        query_string=query_string,
        headers=headers or {},
        body=body,
        **extra,
    )
