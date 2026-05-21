"""Built-in development server (HttpProtocol) — slowloris read timeout (R7)."""

from __future__ import annotations

import asyncio

from veloce import Veloce
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


def test_request_timer_arms_on_first_data():
    """The slowloris guard arms once a request's bytes begin arriving, and
    the idle keep-alive timer is stood down in its favour."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        proto.connection_made(_FakeTransport())

        # Idle connection: keep-alive timer only.
        assert proto._keep_alive_handle is not None
        assert proto._request_timer is None

        # First (incomplete) bytes of a request arrive.
        proto.data_received(b"GET / HTTP/1.1\r\n")
        assert proto._request_timer is not None
        assert proto._keep_alive_handle is None
    finally:
        loop.close()


def test_request_timeout_emits_408_and_closes():
    """When the read budget elapses on a half-sent request, the connection
    is answered with 408 and closed."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _FakeTransport()
        proto.connection_made(transport)
        proto.data_received(b"POST /upload HTTP/1.1\r\nContent-Length: 9999\r\n\r\n")

        proto._request_timeout()  # simulate the timer firing

        assert transport.closed is True
        emitted = b"".join(transport.writes)
        assert b"408" in emitted
        assert proto._request_timer is None
    finally:
        loop.close()


def test_connection_lost_cancels_request_timer():
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        proto.connection_made(_FakeTransport())
        proto.data_received(b"GET / HTTP/1.1\r\n")
        assert proto._request_timer is not None

        proto.connection_lost(None)
        assert proto._request_timer is None
    finally:
        loop.close()
