"""Built-in development server (HttpProtocol) — slowloris read timeout (R7)."""

from __future__ import annotations

import asyncio
import contextlib
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
        # The request was accepted and queued for the server loop; the URL was
        # captured into the snapshot tuple (method, url, headers, body, ka).
        assert any(snap[1] == b"/hello" for snap in proto._request_queue)
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


def _drain_loop(loop: asyncio.AbstractEventLoop, proto: HttpProtocol) -> None:
    """Run the event loop until the connection's server loop finishes."""
    task = proto._server_loop
    if task is not None:
        loop.run_until_complete(task)


def test_pipelined_responses_preserve_request_order():
    """Two requests pipelined in one data_received: the first handler is
    slower than the second, yet response A must be fully written before any of
    response B (HTTP/1.1 FIFO ordering), never interleaved or reordered."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.get("/slow")
        async def slow(request):  # noqa: ANN001, ANN202
            await asyncio.sleep(0.05)
            return {"who": "A"}

        @app.get("/fast")
        async def fast(request):  # noqa: ANN001, ANN202
            return {"who": "B"}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Both requests arrive in a single buffer; slow first, fast second.
        proto.data_received(
            b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\nGET /fast HTTP/1.1\r\nHost: x\r\n\r\n"
        )

        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        a_pos = emitted.find(b'"who":"A"')
        b_pos = emitted.find(b'"who":"B"')
        assert a_pos != -1, "response A was never written"
        assert b_pos != -1, "response B was never written"
        # A's body precedes B's despite A's handler being slower → FIFO held.
        assert a_pos < b_pos
        # And the two responses are not interleaved: A's head/body all land
        # before B's status line.
        b_status = emitted.find(b"HTTP/1.1", a_pos + 1)
        assert b_status > a_pos
        assert b_status < b_pos
    finally:
        loop.close()


def test_split_packet_pipelined_followup_dispatches_with_real_url():
    """Request B's bytes straddle A's dispatch completion (the realistic
    multi-packet pipelining case). A reused parser populates self.url with B's
    URL before A finishes; A's keep-alive _reset must not clobber it. B must be
    served with its real URL, FIFO-ordered after A, with no empty-URL/parse
    error and no 500/504."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        gate = asyncio.Event()

        @app.get("/slow")
        async def slow(request):  # noqa: ANN001, ANN202
            await gate.wait()
            return {"who": "A"}

        @app.get("/fast")
        async def fast(request):  # noqa: ANN001, ANN202
            return {"who": "B"}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # 1. Request A arrives complete; its handler blocks on the gate.
        proto.data_received(b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n")
        loop.run_until_complete(asyncio.sleep(0))
        assert proto._server_loop is not None

        # 2. Request B's bytes arrive in a separate packet while A is still in
        #    flight — the parser writes B's URL into the live buffers.
        proto.data_received(b"GET /fast HTTP/1.1\r\nHost: x\r\n")
        loop.run_until_complete(asyncio.sleep(0))
        assert proto.url == b"/fast", "parser populated the live URL buffer for B"

        # 3. A's handler completes; the keep-alive _reset runs. It must NOT wipe
        #    B's partially-parsed URL out of the shared live buffers.
        gate.set()
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(asyncio.sleep(0))
        assert proto.url == b"/fast", "_reset clobbered B's in-progress URL"

        # 4. B's terminating bytes arrive; B is snapshotted with its real URL.
        proto.data_received(b"\r\n")
        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        a_pos = emitted.find(b'"who":"A"')
        b_pos = emitted.find(b'"who":"B"')
        assert a_pos != -1, "response A was never written"
        assert b_pos != -1, "response B was never written with its real URL"
        assert a_pos < b_pos, "FIFO ordering violated"
        # No empty-URL dispatch surfaced as a 400/500/504.
        assert b"400" not in emitted
        assert b"500" not in emitted
        assert b"504" not in emitted
    finally:
        loop.close()


def test_split_packet_followup_does_not_double_arm_timers():
    """After the first request completes with a partial follow-up buffered, the
    server loop must not arm the idle keep-alive timer on top of the live
    slowloris request timer (keep-alive-XOR-request-timer invariant)."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        gate = asyncio.Event()

        @app.get("/slow")
        async def slow(request):  # noqa: ANN001, ANN202
            await gate.wait()
            return {"who": "A"}

        @app.get("/fast")
        async def fast(request):  # noqa: ANN001, ANN202
            return {"who": "B"}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\n")
        loop.run_until_complete(asyncio.sleep(0))

        # Partial follow-up arrives → slowloris request timer is live.
        proto.data_received(b"GET /fast HTTP/1.1\r\nHost: x\r\n")
        assert proto._request_timer is not None

        # First request finishes; server loop drains and would otherwise rearm
        # the idle timer — but a follow-up is mid-receive, so it must not.
        gate.set()
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(asyncio.sleep(0))

        assert proto._request_timer is not None
        assert proto._keep_alive_handle is None, "idle timer armed while follow-up mid-receive"

        # Finish B so the loop terminates cleanly.
        proto.data_received(b"\r\n")
        _drain_loop(loop, proto)
    finally:
        loop.close()


def test_single_request_dispatches_and_keeps_alive():
    """A single request is served and, being keep-alive, leaves the connection
    open with the server loop finished and the queue drained."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.get("/")
        async def index(request):  # noqa: ANN001, ANN202
            return {"ok": True}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        assert b"200" in emitted
        assert b'"ok":true' in emitted
        assert transport.closed is False
        assert not proto._request_queue
    finally:
        loop.close()


def test_keep_alive_serves_second_sequential_request():
    """After a keep-alive response, a second request on the same connection is
    served correctly (the parser and per-request state are reused)."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.get("/a")
        async def a(request):  # noqa: ANN001, ANN202
            return {"r": "a"}

        @app.get("/b")
        async def b(request):  # noqa: ANN001, ANN202
            return {"r": "b"}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(b"GET /a HTTP/1.1\r\nHost: x\r\n\r\n")
        _drain_loop(loop, proto)
        assert transport.closed is False

        proto.data_received(b"GET /b HTTP/1.1\r\nHost: x\r\n\r\n")
        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        assert b'"r":"a"' in emitted
        assert b'"r":"b"' in emitted
        assert emitted.find(b'"r":"a"') < emitted.find(b'"r":"b"')
        assert transport.closed is False
    finally:
        loop.close()


def test_connection_close_header_closes_after_response():
    """A request with Connection: close is served, then the transport is
    closed and the server loop stops serving."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.get("/one")
        async def one(request):  # noqa: ANN001, ANN202
            return {"n": 1}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(b"GET /one HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        assert b'"n":1' in emitted
        assert transport.closed is True
        assert not proto._request_queue
    finally:
        loop.close()


def test_connection_lost_mid_pipeline_cancels_server_loop():
    """If the client disconnects while the first handler is still running, the
    server loop is cancelled and the queued follow-up is dropped — no wedge."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.get("/slow")
        async def slow(request):  # noqa: ANN001, ANN202
            await asyncio.sleep(1.0)
            return {"who": "A"}

        @app.get("/fast")
        async def fast(request):  # noqa: ANN001, ANN202
            return {"who": "B"}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(
            b"GET /slow HTTP/1.1\r\nHost: x\r\n\r\nGET /fast HTTP/1.1\r\nHost: x\r\n\r\n"
        )

        # Let the server loop start and enter the slow handler's await.
        loop.run_until_complete(asyncio.sleep(0))
        server_loop = proto._server_loop
        assert server_loop is not None

        proto.connection_lost(None)
        assert proto._closing is True
        assert not proto._request_queue

        # Draining the loop must not hang; the server loop ends (cancelled).
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(server_loop)
        assert server_loop.done()
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
