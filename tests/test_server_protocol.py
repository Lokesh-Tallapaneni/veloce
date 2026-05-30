"""Built-in development server (HttpProtocol) — slowloris read timeout (R7)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from veloce import Veloce
from veloce.http._body import DEFAULT_HIGH_WATER_CHUNKS
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
        # Flow-control state + call tallies so backpressure tests can assert
        # pause_reading / resume_reading actually fired.
        self.reading_paused = False
        self.pause_reading_calls = 0
        self.resume_reading_calls = 0

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        self.reading_paused = True
        self.pause_reading_calls += 1

    def resume_reading(self) -> None:
        self.reading_paused = False
        self.resume_reading_calls += 1


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
        # The request was accepted and queued for the server loop at
        # headers-complete; the queue holds (Request, body_source, keep_alive).
        assert any(req.path == "/hello" for req, _src, _ka in proto._request_queue)
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


def _run_until(
    loop: asyncio.AbstractEventLoop,
    predicate: Callable[[], bool],
    *,
    max_turns: int = 100,
) -> None:
    """Drive the loop one scheduling turn at a time until `predicate` holds.

    Lets a parked continuation make progress without depending on the exact
    number of turns a given Python version needs — the loop advances until the
    observable condition is reached (or `max_turns` is exhausted, which fails
    the caller's subsequent assertion rather than hanging)."""
    for _ in range(max_turns):
        if predicate():
            return
        loop.run_until_complete(asyncio.sleep(0))


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


def test_streaming_handler_receives_chunks_as_fed():
    """Dispatch happens at headers-complete; a handler consuming
    request.stream() observes each body chunk at the cadence the protocol
    feeds it, not as one buffered blob after the body fully arrives."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        seen: list[bytes] = []
        first_chunk = asyncio.Event()

        @app.post("/stream")
        async def stream(request):  # noqa: ANN001, ANN202
            async for chunk in request.stream():
                seen.append(chunk)
                if len(seen) == 1:
                    first_chunk.set()
            return {"n": len(seen)}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Headers + declared length arrive first; the body follows in pieces.
        proto.data_received(b"POST /stream HTTP/1.1\r\nHost: x\r\nContent-Length: 6\r\n\r\n")
        loop.run_until_complete(asyncio.sleep(0))
        # Handler dispatched at headers-complete and is now awaiting the body.
        assert proto._server_loop is not None
        assert seen == []

        # First body chunk arrives — the handler must observe it before the
        # rest of the body (and before message-complete).
        proto.data_received(b"abc")
        loop.run_until_complete(first_chunk.wait())
        assert seen == [b"abc"], "chunk not delivered incrementally"

        # Remaining body completes the request.
        proto.data_received(b"def")
        _drain_loop(loop, proto)

        assert seen == [b"abc", b"def"]
        emitted = b"".join(transport.writes)
        assert b'"n":2' in emitted
    finally:
        loop.close()


def test_body_ignoring_handler_does_not_wedge_next_pipelined_request():
    """A handler that ignores the request body must not strand unparsed body
    bytes: the mandatory drain-on-teardown consumes them so the next pipelined
    request parses cleanly and is served FIFO-ordered."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.post("/ignore")
        async def ignore(request):  # noqa: ANN001, ANN202
            # Deliberately never reads the body.
            return {"r": "A"}

        @app.get("/next")
        async def nxt(request):  # noqa: ANN001, ANN202
            return {"r": "B"}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Pipeline: a POST with a body the handler ignores, then a follow-up GET.
        proto.data_received(
            b"POST /ignore HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello"
            b"GET /next HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        a_pos = emitted.find(b'"r":"A"')
        b_pos = emitted.find(b'"r":"B"')
        assert a_pos != -1, "first response missing"
        assert b_pos != -1, "follow-up was wedged by unread body bytes"
        assert a_pos < b_pos, "FIFO ordering violated"
        # The ignored body did not surface as a parse error on the next request.
        assert b"400" not in emitted
        assert b"500" not in emitted
        assert transport.closed is False
    finally:
        loop.close()


def test_body_ignoring_handler_blocks_in_drain_until_eof_then_serves_next_fifo():
    """The production-critical drain path: a body-ignoring handler returns
    while its body is STILL streaming. The server loop reaches source.drain(),
    which must BLOCK awaiting EOF (not exit early), so unparsed body bytes can
    never be misread as the next request. When the remaining body + a pipelined
    follow-up arrive in a LATER data_received, drain unblocks, the body is
    discarded, and the follow-up is served FIFO with no corruption."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.post("/ignore")
        async def ignore(request):  # noqa: ANN001, ANN202
            # Returns immediately without reading the body — drain must wait.
            return {"r": "A"}

        @app.get("/next")
        async def nxt(request):  # noqa: ANN001, ANN202
            return {"r": "B"}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Headers + a PARTIAL body (5 promised, 2 sent). Crucially the rest of
        # the body and the follow-up do NOT arrive in this data_received, so
        # on_message_complete has not fired and EOF is not yet signalled.
        proto.data_received(b"POST /ignore HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhe")
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(asyncio.sleep(0))

        server_loop = proto._server_loop
        assert server_loop is not None
        # The handler returned, its response was queued for write, and the loop
        # is now parked inside source.drain() awaiting EOF — NOT done.
        assert not server_loop.done(), (
            "drain exited before EOF — unread bytes could corrupt next request"
        )
        emitted_so_far = b"".join(transport.writes)
        assert emitted_so_far.find(b'"r":"B"') == -1, "follow-up served before its bytes arrived"

        # Remaining body bytes complete the first request, then the pipelined
        # GET arrives — all in a second data_received. EOF now unblocks drain.
        proto.data_received(b"lloGET /next HTTP/1.1\r\nHost: x\r\n\r\n")
        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        a_pos = emitted.find(b'"r":"A"')
        b_pos = emitted.find(b'"r":"B"')
        assert a_pos != -1, "first response missing"
        assert b_pos != -1, "follow-up was wedged by the blocked drain"
        assert a_pos < b_pos, "FIFO ordering violated"
        # The ignored body's trailing bytes ("llo") were drained, not misread as
        # the next request line.
        assert b"400" not in emitted
        assert b"500" not in emitted
        assert transport.closed is False
    finally:
        loop.close()


def test_connection_lost_unblocks_a_drain_awaiting_eof():
    """The deadlock backstop: if the client disconnects while the server loop
    is parked in source.drain() awaiting body bytes that will never arrive,
    connection_lost(None) must feed EOF so the blocked drain unblocks and the
    server-loop task ends (cancelled) rather than hanging forever."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.post("/ignore")
        async def ignore(request):  # noqa: ANN001, ANN202
            return {"r": "A"}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Headers + partial body; the rest never comes. Handler returns, server
        # loop parks in drain() awaiting EOF.
        proto.data_received(b"POST /ignore HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhe")
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(asyncio.sleep(0))

        server_loop = proto._server_loop
        assert server_loop is not None
        assert not server_loop.done(), "expected the loop parked in drain awaiting EOF"

        # Client vanishes mid-drain. connection_lost feeds EOF to the in-flight
        # source (unblocking drain) and cancels the server loop.
        proto.connection_lost(None)
        # Must not hang: the blocked drain wakes on the EOF / cancellation.
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(server_loop)
        assert server_loop.done()
    finally:
        loop.close()


def test_oversized_streamed_body_rejected_413_mid_stream():
    """An over-limit body is refused 413 once the streamed running total
    crosses MAX_CONTENT_LENGTH — before the whole body is read — and the
    connection is closed."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["MAX_CONTENT_LENGTH"] = 8

        @app.post("/u")
        async def upload(request):  # noqa: ANN001, ANN202
            body = await request.body()
            return {"len": len(body)}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # No declared Content-Length up front (chunked-style) so the cap is
        # enforced against the streamed running total, not the header.
        proto.data_received(b"POST /u HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n")
        loop.run_until_complete(asyncio.sleep(0))
        # Feed a body chunk that overflows the 8-byte cap.
        proto.data_received(b"5\r\nhello\r\n4\r\nmore\r\n0\r\n\r\n")
        with contextlib.suppress(asyncio.CancelledError):
            _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        assert b"413" in emitted
        assert b"Content Too Large" in emitted
        assert transport.closed is True
    finally:
        loop.close()


def test_declared_content_length_over_limit_rejected_413_before_body():
    """An honest client announcing an over-limit upload via Content-Length is
    refused 413 at headers-complete, before any body byte is read."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["MAX_CONTENT_LENGTH"] = 8

        @app.post("/u")
        async def upload(request):  # noqa: ANN001, ANN202
            return {"ok": True}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(b"POST /u HTTP/1.1\r\nHost: x\r\nContent-Length: 9999\r\n\r\n")

        emitted = b"".join(transport.writes)
        assert b"413" in emitted
        assert transport.closed is True
        # Rejected before dispatch — no request was queued for the server loop.
        assert not proto._request_queue
    finally:
        loop.close()


def test_slowloris_timer_arms_across_body_window():
    """With headers-complete dispatch the body window is now part of the same
    request, so the slowloris timer must remain armed after headers parse and
    still fire on a stalled body (verifying the timer spans the longer window)."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        body_started = asyncio.Event()

        @app.post("/u")
        async def upload(request):  # noqa: ANN001, ANN202
            body_started.set()
            # Block reading the body — the client stalls mid-upload.
            return {"len": len(await request.body())}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Headers arrive and complete; body is promised but never sent.
        proto.data_received(b"POST /u HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n")
        # The slowloris guard armed on the first bytes is still live across the
        # body window (not cancelled at headers-complete), and the idle
        # keep-alive timer is stood down.
        assert proto._request_timer is not None
        assert proto._keep_alive_handle is None

        # Simulate the stalled-body timeout firing.
        proto._request_timeout()
        assert transport.closed is True
        emitted = b"".join(transport.writes)
        assert b"408" in emitted

        # The transport close drives connection_lost, which cancels the
        # server loop (the handler was blocked awaiting a body that never
        # finished) — mirror that here so no task is left pending.
        proto.connection_lost(None)
        server_loop = proto._server_loop
        if server_loop is not None:
            with contextlib.suppress(asyncio.CancelledError):
                loop.run_until_complete(server_loop)
    finally:
        loop.close()


def test_slow_consumer_triggers_pause_then_resume_across_reads():
    """A producer feeding more chunks than the buffer bound across successive
    socket reads, ahead of a slow consumer, must trigger transport.pause_reading
    once the high-water mark is reached, and transport.resume_reading once the
    consumer drains back below the low-water mark. This models the cross-read
    bound: a paused socket stops delivering *future* reads."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        high = DEFAULT_HIGH_WATER_CHUNKS

        release = asyncio.Event()

        @app.post("/stream")
        async def stream(request):  # noqa: ANN001, ANN202
            # Hold off consuming until the producer has fed past the bound, so
            # the buffer fills and pause_reading is forced to fire.
            await release.wait()
            n = 0
            async for _chunk in request.stream():
                n += 1
            return {"n": n}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Dispatch at headers-complete; the handler parks on `release`.
        proto.data_received(
            b"POST /stream HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        loop.run_until_complete(asyncio.sleep(0))
        source = proto._current_source
        assert source is not None

        # One chunk per read (modelling separate socket reads): a real paused
        # socket stops delivering, so we stop feeding the instant pause engages.
        for _ in range(high * 4):
            proto.on_body(b"z")
            if transport.reading_paused:
                break

        assert transport.pause_reading_calls >= 1, "pause_reading never fired"
        # Modelling a real socket (which stops delivering once paused) we ceased
        # feeding the instant pause engaged, so the buffer settled at the bound.
        # This asserts the *cross-read* behaviour: future reads stop piling up
        # once paused. It does NOT claim a single read is capped — that case is
        # exercised by test_single_segment_burst_exceeds_chunk_bound below.
        assert len(source._chunks) <= high

        # Let the consumer drain. It pops chunks, crosses the low-water mark,
        # and resume_reading fires.
        release.set()
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(asyncio.sleep(0))
        assert transport.resume_reading_calls >= 1, "resume_reading never fired after drain"
        assert transport.reading_paused is False

        # Finish the request so the loop terminates cleanly.
        proto.on_message_complete()
        _drain_loop(loop, proto)
        emitted = b"".join(transport.writes)
        assert b"200" in emitted
    finally:
        loop.close()


def test_single_segment_burst_exceeds_chunk_bound_but_byte_cap_holds():
    """Pausing does not cap a single read: many chunked frames arriving in one
    data_received (one TCP segment) are all fed in one parser pass before the
    handler runs, so the buffered chunk count can exceed the high-water mark.
    The honest memory bound on a single segment is MAX_CONTENT_LENGTH — once the
    running byte total passes it the source latches overflow and stops buffering,
    which is what actually protects RAM."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        high = DEFAULT_HIGH_WATER_CHUNKS
        # Byte cap below the burst's total so overflow latches mid-segment.
        app.config["MAX_CONTENT_LENGTH"] = high  # high one-byte chunks fit; the rest overflow

        release = asyncio.Event()

        @app.post("/stream")
        async def stream(request):  # noqa: ANN001, ANN202
            await release.wait()
            async for _chunk in request.stream():
                pass
            return {"ok": True}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(
            b"POST /stream HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        loop.run_until_complete(asyncio.sleep(0))
        source = proto._current_source
        assert source is not None

        # Many frames in ONE socket read. pause_reading only stops FUTURE reads,
        # so every frame in this segment is fed in a single synchronous pass: the
        # chunk count is free to overshoot the high-water bound (which is exactly
        # why the chunk count is NOT the memory guarantee). The byte cap is the
        # real backstop — once the running total passes MAX_CONTENT_LENGTH the
        # source latches overflow and drops further chunks instead of buffering.
        for _ in range(high * 4):
            proto.on_body(b"z")

        # Without the byte cap this single burst would have buffered high*4 chunks
        # (4x the bound). With it, buffering stopped at the cap: the high allowed
        # bytes are queued, everything past the cap is dropped, not buffered.
        assert source._overflow is True, "byte cap should have latched mid-burst"
        assert len(source._chunks) <= high, "byte cap must bound the buffer within one read"

        release.set()
        _drain_loop(loop, proto)
        emitted = b"".join(transport.writes)
        assert b"413" in emitted
    finally:
        loop.close()


def test_drain_resumes_then_second_burst_repauses_still_reaches_eof():
    """Regression for the drain deadlock: a body-ignoring handler returns while
    the connection is paused; drain() resumes once. If a SECOND >high_water
    burst then arrives on a later socket read (which under the old one-time
    resume would re-pause and never resume again), drain must still reach EOF
    and the next pipelined request must be served — no hang."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        high = DEFAULT_HIGH_WATER_CHUNKS

        # Gate the body-ignoring handler so it is provably still in-flight (not
        # yet returned/drained) at the moment we assert the pause. This removes
        # the dependence on how many sleep(0) turns a given Python version needs
        # to schedule the handler to completion.
        gate = asyncio.Event()

        @app.post("/ignore")
        async def ignore(request):  # noqa: ANN001, ANN202
            await gate.wait()
            return {"r": "A"}

        @app.get("/next")
        async def nxt(request):  # noqa: ANN001, ANN202
            return {"r": "B"}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(
            b"POST /ignore HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        loop.run_until_complete(asyncio.sleep(0))
        source = proto._current_source
        assert source is not None

        # First burst across reads trips the high-water mark and pauses. The
        # handler is parked on the gate, so it cannot have returned and drained:
        # the pause we observe is the producer-side high-water pause, full stop.
        for _ in range(high):
            proto.data_received(b"1\r\nz\r\n")
        assert transport.reading_paused is True
        pause_calls_after_first = transport.pause_reading_calls

        # Release the gate: the handler returns and the server loop parks in
        # drain(), which resumes the paused socket so more body can flow. Drive
        # until the resume is observable rather than counting scheduling turns.
        gate.set()
        _run_until(loop, lambda: not transport.reading_paused)
        assert transport.reading_paused is False, "drain did not resume the paused socket"
        resume_calls_after_first = transport.resume_reading_calls
        assert resume_calls_after_first >= 1

        # SECOND burst on a later read. Under the old one-time-resume drain this
        # would re-pause (pause_reading_calls bumps, reading_paused True) and
        # never resume again — wedging the connection. With _draining latched,
        # feed() must NOT re-pause: the socket stays unpaused through the drain.
        for _ in range(high * 2):
            proto.data_received(b"1\r\nz\r\n")
        loop.run_until_complete(asyncio.sleep(0))
        assert transport.pause_reading_calls == pause_calls_after_first, (
            "feed() re-paused mid-drain — the connection will never resume and EOF never arrives"
        )
        assert transport.reading_paused is False, "connection re-paused during drain (deadlock)"
        # The byte buffer stays bounded during drain: feed() discards while draining.
        assert len(source._chunks) == 0

        # Terminating chunk + pipelined follow-up. drain reaches EOF, B is served.
        proto.data_received(b"0\r\n\r\nGET /next HTTP/1.1\r\nHost: x\r\n\r\n")
        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        a_pos = emitted.find(b'"r":"A"')
        b_pos = emitted.find(b'"r":"B"')
        assert a_pos != -1, "first response missing"
        assert b_pos != -1, "follow-up wedged by a paused, never-resumed connection"
        assert a_pos < b_pos, "FIFO ordering violated"
        assert b"400" not in emitted
        assert b"500" not in emitted
        assert transport.closed is False
    finally:
        loop.close()


def test_paused_connection_with_body_ignoring_handler_resumes_and_drains():
    """Backpressure must not deadlock a handler that ignores its body. After
    the buffer fills and reading pauses, a returning handler reaches
    source.drain(); drain must resume reading so the remaining body can arrive,
    reach EOF, and the next pipelined request is served FIFO."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        high = DEFAULT_HIGH_WATER_CHUNKS

        # Gate the body-ignoring handler so it is provably parked (not yet
        # returned/drained) when we assert the high-water pause. Otherwise a
        # faster-scheduling Python (3.12/3.13) lets the handler return and drain
        # — resuming reading — before the assertion runs.
        gate = asyncio.Event()

        @app.post("/ignore")
        async def ignore(request):  # noqa: ANN001, ANN202
            await gate.wait()
            return {"r": "A"}

        @app.get("/next")
        async def nxt(request):  # noqa: ANN001, ANN202
            return {"r": "B"}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Chunked body so each frame surfaces as a separate on_body chunk;
        # feeding more frames than the bound (while the handler ignores the
        # body) forces a pause. Bytes go through the real parser so it stays in
        # sync for the pipelined follow-up.
        proto.data_received(
            b"POST /ignore HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        loop.run_until_complete(asyncio.sleep(0))
        source = proto._current_source
        assert source is not None

        # Feed enough one-byte chunk frames to trip the high-water mark. The
        # handler is parked on the gate, so it cannot have drained — this is the
        # producer-side high-water pause.
        for _ in range(high):
            proto.data_received(b"1\r\nz\r\n")
        assert transport.reading_paused is True, "expected reading paused at the bound"

        # Release the gate: the handler returns and the server loop parks in
        # source.drain() awaiting EOF. drain must resume reading so more body can
        # flow — otherwise a real socket would never deliver EOF. Drive until the
        # resume is observable rather than counting scheduling turns.
        gate.set()
        _run_until(loop, lambda: not transport.reading_paused)
        assert transport.reading_paused is False, "drain did not resume a paused connection"

        # The terminating chunk + the pipelined follow-up arrive; drain
        # discards the body, EOF unblocks it, and B is served FIFO.
        proto.data_received(b"0\r\n\r\nGET /next HTTP/1.1\r\nHost: x\r\n\r\n")
        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        a_pos = emitted.find(b'"r":"A"')
        b_pos = emitted.find(b'"r":"B"')
        assert a_pos != -1, "first response missing"
        assert b_pos != -1, "follow-up wedged by a paused, never-resumed connection"
        assert a_pos < b_pos, "FIFO ordering violated"
        assert b"400" not in emitted
        assert b"500" not in emitted
        assert transport.closed is False
    finally:
        loop.close()


def test_connection_lost_while_paused_unblocks_drain():
    """A paused connection torn down mid-drain must still unblock: connection_lost
    feeds EOF, the parked drain wakes, and the server loop ends rather than
    hanging behind a pause that will never be resumed by more bytes."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        high = DEFAULT_HIGH_WATER_CHUNKS

        # Park the handler on a gate it never gets to pass: the connection stays
        # genuinely paused at the high-water mark (the handler has not returned
        # to drain and resume), so the assertion below holds on every Python
        # version rather than racing the handler to completion.
        gate = asyncio.Event()

        @app.post("/ignore")
        async def ignore(request):  # noqa: ANN001, ANN202
            await gate.wait()
            return {"r": "A"}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(
            b"POST /ignore HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        )
        loop.run_until_complete(asyncio.sleep(0))
        source = proto._current_source
        assert source is not None
        for _ in range(high):
            proto.on_body(b"z")
        assert transport.reading_paused is True

        server_loop = proto._server_loop
        assert server_loop is not None

        # Client vanishes while the connection is paused and the handler is
        # still in-flight — no more bytes will ever arrive to resume it.
        proto.connection_lost(None)
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(server_loop)
        assert server_loop.done()
    finally:
        loop.close()


def test_streaming_handler_timeout_does_not_race_drain_with_live_consumer():
    """F1 regression: a handler parked in `async for chunk in request.stream()`
    that sleeps past REQUEST_HANDLER_TIMEOUT must time out cleanly with a 504.

    The shielded handler stays alive after wait_for raises, still the sole
    consumer awaiting the body source. The dispatch path must NOT drain that
    same source inline (a second waiter racing the live consumer on the source's
    single-waiter event), so the read is never truncated and the buffer never
    thrashes. The connection is closed (not reused), and when the detached
    handler finally unwinds — here via connection_lost feeding EOF, mirroring a
    real transport close — there is no 'coroutine never awaited' warning, no
    event-loop error, and no second drain corrupting state."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["REQUEST_HANDLER_TIMEOUT"] = 0.02

        seen: list[bytes] = []
        handler_finished = asyncio.Event()

        @app.post("/stream")
        async def stream(request):  # noqa: ANN001, ANN202
            try:
                async for chunk in request.stream():
                    seen.append(chunk)
                    # Park well past the handler timeout while still the source's
                    # sole consumer — this is the window the inline drain raced.
                    await asyncio.sleep(0.2)
            finally:
                handler_finished.set()
            return {"chunks": len(seen)}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Headers complete → handler dispatched; one body chunk arrives so the
        # consumer is parked inside its sleep when the timeout fires.
        proto.data_received(b"POST /stream HTTP/1.1\r\nHost: x\r\nContent-Length: 6\r\n\r\n")
        loop.run_until_complete(asyncio.sleep(0))
        proto.on_body(b"abc")
        loop.run_until_complete(asyncio.sleep(0))
        assert seen == [b"abc"], "consumer should have observed the first chunk"

        server_loop = proto._server_loop
        assert server_loop is not None

        # Let the handler timeout elapse; the server loop produces the 504 and
        # returns (closing the connection) without draining the live source.
        loop.run_until_complete(server_loop)

        emitted = b"".join(transport.writes)
        assert b"504" in emitted
        assert b"Gateway Timeout" in emitted
        assert transport.closed is True, "timed-out connection must be closed, not reused"

        # The shielded handler is still alive (still parked in its sleep loop);
        # the inline drain never ran, so its read was not truncated.
        assert not handler_finished.is_set()
        # The buffered first chunk was not discarded by a racing drain.
        assert seen == [b"abc"]

        # Mirror the real transport close: connection_lost feeds EOF, unblocking
        # the detached handler so it unwinds. The deferred done-callback then
        # drains the (already-EOF) source. Nothing must hang or error.
        proto.connection_lost(None)
        loop.run_until_complete(handler_finished.wait())
        # Flush the deferred drain task scheduled by the done-callback.
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(asyncio.sleep(0))

        # No corruption: only the one chunk was ever seen, no extra error written.
        assert seen == [b"abc"]
        assert b"500" not in emitted
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


def test_serve_loop_stops_at_boundary_when_keep_serving_false():
    """When should_keep_serving returns False (worker recycling tripped), the
    serve loop dispatches the in-flight request, then stops at the boundary and
    closes the connection — a queued/pipelined follow-up is NOT dispatched past
    the max_requests limit."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        served: list[str] = []

        @app.get("/a")
        async def a(request):  # noqa: ANN001, ANN202
            served.append("a")
            return {"r": "a"}

        @app.get("/b")
        async def b(request):  # noqa: ANN001, ANN202
            served.append("b")
            return {"r": "b"}

        # Recycling tripped before serving: stop after the current request.
        HttpProtocol.should_keep_serving = lambda: False
        try:
            proto = HttpProtocol(app, loop)
            transport = _FakeTransport()
            proto.connection_made(transport)

            # Two pipelined requests arrive together.
            proto.data_received(
                b"GET /a HTTP/1.1\r\nHost: x\r\n\r\nGET /b HTTP/1.1\r\nHost: x\r\n\r\n"
            )
            _drain_loop(loop, proto)

            emitted = b"".join(transport.writes)
            # The first request was served; the second must NOT have been.
            assert served == ["a"]
            assert b'"r":"a"' in emitted
            assert b'"r":"b"' not in emitted
            # The connection is closed so the client reconnects to a fresh worker.
            assert transport.closed is True
        finally:
            HttpProtocol.should_keep_serving = None
    finally:
        loop.close()


def test_serve_loop_continues_when_keep_serving_true():
    """A should_keep_serving predicate returning True does not change keep-alive
    behaviour: both pipelined requests are served on one connection."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.get("/a")
        async def a(request):  # noqa: ANN001, ANN202
            return {"r": "a"}

        @app.get("/b")
        async def b(request):  # noqa: ANN001, ANN202
            return {"r": "b"}

        HttpProtocol.should_keep_serving = lambda: True
        try:
            proto = HttpProtocol(app, loop)
            transport = _FakeTransport()
            proto.connection_made(transport)

            proto.data_received(
                b"GET /a HTTP/1.1\r\nHost: x\r\n\r\nGET /b HTTP/1.1\r\nHost: x\r\n\r\n"
            )
            _drain_loop(loop, proto)

            emitted = b"".join(transport.writes)
            assert b'"r":"a"' in emitted
            assert b'"r":"b"' in emitted
            assert transport.closed is False
        finally:
            HttpProtocol.should_keep_serving = None
    finally:
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


def test_expect_100_continue_emits_interim_before_response():
    """An HTTP/1.1 client sending `Expect: 100-continue` is cleared with an
    interim `100 Continue` at headers-complete, before its body arrives, and
    the final response follows once the body is sent (RFC 9110 section
    10.1.1)."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.post("/u")
        async def upload(request):  # noqa: ANN001, ANN202
            return {"len": len(await request.body())}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Headers complete with the Expect header but no body yet.
        proto.data_received(
            b"POST /u HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\nContent-Length: 3\r\n\r\n"
        )
        interim = b"".join(transport.writes)
        assert b"HTTP/1.1 100 Continue\r\n\r\n" in interim
        # Only the interim has been written so far — no final response.
        assert b"200" not in interim

        # The client, now cleared, sends the body; the final response follows.
        proto.data_received(b"abc")
        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        interim_pos = emitted.find(b"HTTP/1.1 100 Continue\r\n\r\n")
        final_pos = emitted.find(b"200")
        assert interim_pos != -1
        assert final_pos != -1
        assert interim_pos < final_pos, "interim must precede the final response"
        assert b'"len":3' in emitted
    finally:
        loop.close()


def test_no_expect_header_does_not_emit_interim():
    """A request without `Expect: 100-continue` gets no interim response."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.post("/u")
        async def upload(request):  # noqa: ANN001, ANN202
            return {"ok": True}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(b"POST /u HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n\r\nabc")
        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        assert b"100 Continue" not in emitted
        assert b"200" in emitted
    finally:
        loop.close()


def test_expect_100_continue_not_sent_to_http_10_client():
    """RFC 9110 section 10.1.1: a server must not send a 100 Continue to an
    HTTP/1.0 client, even when it carries `Expect: 100-continue`."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)

        @app.post("/u")
        async def upload(request):  # noqa: ANN001, ANN202
            return {"ok": True}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(
            b"POST /u HTTP/1.0\r\nHost: x\r\nExpect: 100-continue\r\nContent-Length: 3\r\n\r\nabc"
        )
        _drain_loop(loop, proto)

        emitted = b"".join(transport.writes)
        assert b"100 Continue" not in emitted
    finally:
        loop.close()


def test_expect_100_continue_over_limit_yields_413_not_interim():
    """An over-limit declared Content-Length is refused 413 before any interim
    is sent — we never invite a body we are about to reject."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["MAX_CONTENT_LENGTH"] = 8

        @app.post("/u")
        async def upload(request):  # noqa: ANN001, ANN202
            return {"ok": True}

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        proto.data_received(
            b"POST /u HTTP/1.1\r\nHost: x\r\nExpect: 100-continue\r\nContent-Length: 9999\r\n\r\n"
        )

        emitted = b"".join(transport.writes)
        assert b"413" in emitted
        assert b"100 Continue" not in emitted
        assert transport.closed is True
        assert not proto._request_queue
    finally:
        loop.close()


def test_request_timeout_honours_config_override():
    """`REQUEST_TIMEOUT` in app.config shortens the slowloris read budget so a
    half-sent request is dropped with 408 sooner than the 30s default."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["REQUEST_TIMEOUT"] = 0.02

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Partial request: headers never complete, so the request timer keeps
        # running until the (overridden, very short) budget elapses.
        proto.data_received(b"POST /upload HTTP/1.1\r\nContent-Length: 9999\r\n")
        assert proto._request_timer is not None

        loop.run_until_complete(asyncio.sleep(0.05))

        assert transport.closed is True
        emitted = b"".join(transport.writes)
        assert b"408" in emitted
        assert proto._request_timer is None
    finally:
        loop.close()


def test_keep_alive_timeout_honours_config_override():
    """`KEEP_ALIVE_TIMEOUT` in app.config closes an idle keep-alive connection
    sooner than the 75s default."""
    loop = asyncio.new_event_loop()
    try:
        app = Veloce(openapi_url=None)
        app.config["KEEP_ALIVE_TIMEOUT"] = 0.02

        proto = HttpProtocol(app, loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Idle connection: only the keep-alive timer is armed.
        assert proto._keep_alive_handle is not None
        assert proto._request_timer is None

        loop.run_until_complete(asyncio.sleep(0.05))

        assert transport.closed is True
    finally:
        loop.close()


def test_timeout_defaults_unchanged():
    """The class-attribute defaults and the seeded config keys both stay at the
    documented 75s / 30s, so an app that sets neither override is unaffected."""
    from veloce.config import Config

    assert HttpProtocol.KEEP_ALIVE_TIMEOUT == 75
    assert HttpProtocol.REQUEST_TIMEOUT == 30

    defaults = Config.default_config()
    assert defaults["KEEP_ALIVE_TIMEOUT"] == 75
    assert defaults["REQUEST_TIMEOUT"] == 30
