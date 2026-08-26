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
) -> Request:
    """Factory for test Request objects."""
    return Request(
        method=method,
        path=path,
        query_string=query_string,
        headers=headers or {},
        body=body,
    )
