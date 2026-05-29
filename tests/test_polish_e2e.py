"""End-to-end regression tests for the polish-wave-2 fixes.

Covers:
- #36m: HttpProtocol MAX_CONCURRENT_CONNECTIONS cap and slot release.
- #36: deprecation warnings on `on_event` / `add_event_handler`.
- #60: `Response.iter_encoded()` sync vs async duality.
"""

from __future__ import annotations

import asyncio
import contextlib
import warnings
from collections.abc import AsyncIterator, Iterator

import pytest

from veloce import Veloce
from veloce.http.response import Response, StreamingResponse
from veloce.serving.protocol import HttpProtocol


class _FakeTransport(asyncio.Transport):
    """Minimal asyncio.Transport stand-in for protocol unit tests."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


def _reset_connection_counter() -> None:
    """Pin the class counter to 0 so test ordering doesn't leak state."""
    with HttpProtocol._connections_lock:
        HttpProtocol._active_connections = 0


# ── #36m: connection limit ──────────────────────────────────────────


def test_third_concurrent_connection_gets_503_and_closed():
    _reset_connection_counter()
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["MAX_CONCURRENT_CONNECTIONS"] = 2

        admitted: list[HttpProtocol] = []
        for _ in range(2):
            proto = HttpProtocol(app, loop)
            transport = _FakeTransport()
            proto.connection_made(transport)
            assert transport.closed is False
            assert proto._counted is True
            admitted.append(proto)

        assert HttpProtocol._active_connections == 2

        rejected = HttpProtocol(app, loop)
        rejected_transport = _FakeTransport()
        rejected.connection_made(rejected_transport)

        emitted = b"".join(rejected_transport.writes)
        assert b"HTTP/1.1 503" in emitted
        assert b"Service Unavailable" in emitted
        assert b"Connection: close" in emitted
        assert rejected_transport.closed is True
        assert rejected._counted is False
        assert HttpProtocol._active_connections == 2
    finally:
        for proto in admitted:
            proto.connection_lost(None)
        _reset_connection_counter()
        loop.close()


def test_disconnect_releases_slot_for_new_connection():
    _reset_connection_counter()
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["MAX_CONCURRENT_CONNECTIONS"] = 2

        proto_a = HttpProtocol(app, loop)
        proto_a.connection_made(_FakeTransport())
        proto_b = HttpProtocol(app, loop)
        proto_b.connection_made(_FakeTransport())
        assert HttpProtocol._active_connections == 2

        proto_a.connection_lost(None)
        proto_b.connection_lost(None)
        assert HttpProtocol._active_connections == 0

        proto_d = HttpProtocol(app, loop)
        d_transport = _FakeTransport()
        proto_d.connection_made(d_transport)

        assert d_transport.closed is False
        assert proto_d._counted is True
        assert HttpProtocol._active_connections == 1
        emitted = b"".join(d_transport.writes)
        assert b"503" not in emitted
    finally:
        with contextlib.suppress(Exception):
            proto_d.connection_lost(None)
        _reset_connection_counter()
        loop.close()


# ── #36: lifecycle hook deprecation warnings ────────────────────────


def test_on_event_decorator_emits_deprecation_warning():
    app = Veloce(openapi_url=None)
    with pytest.warns(DeprecationWarning, match="on_startup"):

        @app.on_event("startup")
        async def boot() -> None:
            return None

    assert boot in app._on_startup
    assert callable(boot)


def test_add_event_handler_emits_deprecation_warning():
    app = Veloce(openapi_url=None)

    async def boot() -> None:
        return None

    with pytest.warns(DeprecationWarning, match="on_startup"):
        app.add_event_handler("startup", boot)

    assert boot in app._on_startup


def test_on_startup_decorator_does_not_warn():
    app = Veloce(openapi_url=None)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)

        @app.on_startup
        async def boot() -> None:
            return None

    assert boot in app._on_startup


# ── #60: iter_encoded duality ───────────────────────────────────────


def test_iter_encoded_returns_sync_iterator_for_buffered_response():
    resp = Response(body=b"hello")
    assert resp.is_streamed is False

    iterator = resp.iter_encoded()

    assert not asyncio.iscoroutine(iterator)
    assert not hasattr(iterator, "__aiter__"), "buffered iter must be sync"
    assert isinstance(iterator, Iterator)
    chunks = list(iterator)
    assert chunks == [b"hello"]


def test_iter_encoded_returns_async_iterator_for_streaming_response():
    async def gen() -> AsyncIterator[bytes]:
        yield b"a"
        yield b"b"

    resp = StreamingResponse(gen())
    assert resp.is_streamed is True

    iterator = resp.iter_encoded()

    assert not asyncio.iscoroutine(iterator)
    assert hasattr(iterator, "__aiter__"), "streaming iter must be async"

    async def _drain() -> list[bytes]:
        return [chunk async for chunk in iterator]

    loop = asyncio.new_event_loop()
    try:
        chunks = loop.run_until_complete(_drain())
    finally:
        loop.close()
    assert chunks == [b"a", b"b"]
