"""Built-in development server (HttpProtocol) — slowloris read timeout (R7)."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from veloce import Veloce
from veloce.serving.protocol import (
    MAX_HEADER_SIZE,
    MAX_TOTAL_HEADERS_SIZE,
    MAX_URL_SIZE,
    HttpProtocol,
)


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


def test_oversized_url_emits_414_and_closes():
    """A request-line URL longer than MAX_URL_SIZE is rejected with 414."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        long_path = b"/" + (b"a" * (MAX_URL_SIZE + 1))
        proto.data_received(b"GET " + long_path + b" HTTP/1.1\r\nHost: x\r\n\r\n")

        emitted = b"".join(transport.writes)
        assert b"414" in emitted
        assert b"URI Too Long" in emitted
        assert b"Connection: close" in emitted
        assert b"Content-Length: 0" in emitted
        assert transport.closed is True
        assert proto._oversized is True
    finally:
        loop.close()


def test_oversized_single_header_emits_431_and_closes():
    """A single header field whose name+value exceeds MAX_HEADER_SIZE → 431."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        big_value = b"v" * (MAX_HEADER_SIZE + 1)
        proto.data_received(b"GET / HTTP/1.1\r\nHost: x\r\nX-Huge: " + big_value + b"\r\n\r\n")

        emitted = b"".join(transport.writes)
        assert b"431" in emitted
        assert b"Request Header Fields Too Large" in emitted
        assert b"Connection: close" in emitted
        assert transport.closed is True
    finally:
        loop.close()


def test_cumulative_headers_exceeds_total_cap_emits_431():
    """Many medium headers whose cumulative size exceeds the total cap → 431."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Each header is under MAX_HEADER_SIZE individually, but together
        # they overflow MAX_TOTAL_HEADERS_SIZE.
        per_value = b"v" * 1024
        count = (MAX_TOTAL_HEADERS_SIZE // (len(per_value) + 8)) + 4
        headers = b"".join(
            f"X-Pad-{i:04d}: ".encode("ascii") + per_value + b"\r\n" for i in range(count)
        )
        proto.data_received(b"GET / HTTP/1.1\r\nHost: x\r\n" + headers + b"\r\n")

        emitted = b"".join(transport.writes)
        assert b"431" in emitted
        assert transport.closed is True
        assert proto._header_bytes_total <= MAX_TOTAL_HEADERS_SIZE
    finally:
        loop.close()


def test_normal_small_request_is_not_rejected():
    """Normal-sized requests still parse without tripping the new caps."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(b"GET /hello HTTP/1.1\r\nHost: x\r\nX-Foo: bar\r\n\r\n")

        # No oversized rejection; no error response written before dispatch.
        assert proto._oversized is False
        emitted = b"".join(transport.writes)
        assert b"414" not in emitted
        assert b"431" not in emitted
        # The captured URL & headers were accepted by the callbacks.
        assert b"/hello" in proto.url or proto.request_complete
    finally:
        loop.close()


def test_connection_closed_after_oversized_rejection():
    """After the error response, the transport must be closed."""
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        big_value = b"v" * (MAX_HEADER_SIZE + 1)
        proto.data_received(b"GET / HTTP/1.1\r\nHost: x\r\nX-Huge: " + big_value + b"\r\n\r\n")

        assert transport.closed is True
        # Subsequent data_received calls are short-circuited and do not
        # write another response.
        writes_before = len(transport.writes)
        proto.data_received(b"more junk")
        assert len(transport.writes) == writes_before
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


def _reset_connection_counter() -> None:
    """Pin the class counter to 0 so test ordering doesn't leak state."""
    with HttpProtocol._connections_lock:
        HttpProtocol._active_connections = 0


def test_connection_limit_emits_503():
    """When the cap is reached, additional connections receive 503 and close."""
    _reset_connection_counter()
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["MAX_CONCURRENT_CONNECTIONS"] = 1

        proto1 = HttpProtocol(app, loop)
        t1 = _FakeTransport()
        proto1.connection_made(t1)
        assert t1.closed is False
        assert proto1._counted is True
        assert HttpProtocol._active_connections == 1

        proto2 = HttpProtocol(app, loop)
        t2 = _FakeTransport()
        proto2.connection_made(t2)
        emitted = b"".join(t2.writes)
        assert b"503" in emitted
        assert b"Service Unavailable" in emitted
        assert b"Connection: close" in emitted
        assert b"Content-Length: 0" in emitted
        assert t2.closed is True
        assert proto2._counted is False
        # Counter was NOT incremented past the cap.
        assert HttpProtocol._active_connections == 1
    finally:
        proto1.connection_lost(None)
        _reset_connection_counter()
        loop.close()


def test_connection_limit_releases_on_disconnect():
    """connection_lost decrements the counter so freed slots get reused."""
    _reset_connection_counter()
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["MAX_CONCURRENT_CONNECTIONS"] = 1

        proto1 = HttpProtocol(app, loop)
        proto1.connection_made(_FakeTransport())
        assert HttpProtocol._active_connections == 1

        proto1.connection_lost(None)
        assert HttpProtocol._active_connections == 0
        assert proto1._counted is False

        # A rejected connection's connection_lost must not under-flow the counter.
        proto2 = HttpProtocol(app, loop)
        proto2.connection_made(_FakeTransport())
        assert HttpProtocol._active_connections == 1

        proto3 = HttpProtocol(app, loop)
        t3 = _FakeTransport()
        proto3.connection_made(t3)
        assert t3.closed is True
        assert HttpProtocol._active_connections == 1
        proto3.connection_lost(None)
        assert HttpProtocol._active_connections == 1
    finally:
        proto2.connection_lost(None)
        _reset_connection_counter()
        loop.close()


def test_connection_count_is_thread_safe():
    """Parallel connection_made calls under the lock never over-admit."""
    _reset_connection_counter()
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        cap = 50
        app.config["MAX_CONCURRENT_CONNECTIONS"] = cap

        protos: list[HttpProtocol] = []
        transports: list[_FakeTransport] = []
        for _ in range(500):
            protos.append(HttpProtocol(app, loop))
            transports.append(_FakeTransport())

        def _do(i: int) -> None:
            protos[i].connection_made(transports[i])

        with ThreadPoolExecutor(max_workers=32) as ex:
            list(ex.map(_do, range(len(protos))))

        # Counter never exceeds the cap.
        assert HttpProtocol._active_connections == cap
        admitted = sum(1 for p in protos if p._counted)
        rejected = sum(1 for t in transports if t.closed)
        assert admitted == cap
        assert admitted + rejected == len(protos)
    finally:
        for p in protos:
            if p._counted:
                p.connection_lost(None)
        _reset_connection_counter()
        loop.close()
