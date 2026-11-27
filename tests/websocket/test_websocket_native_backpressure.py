"""Native (raw-transport) WebSocket outbound backpressure.

The native WebSocket send path awaits `HttpProtocol.drain()` before writing each
frame. When the transport's outgoing buffer crosses its high-water mark the event
loop calls `HttpProtocol.pause_writing()`, which closes the drain gate; a producing
handler then suspends at its next send instead of letting the transport buffer the
frame in memory. `resume_writing()` reopens the gate once the buffer drains.

These tests drive `HttpProtocol` directly through a genuine RFC 6455 handshake and
real frames over a controllable fake transport, toggling `pause_writing` /
`resume_writing` exactly as the event loop would for a slow-reading peer. They
assert the producer parks while paused (no unbounded write-buffer growth) and
resumes once the gate reopens. ASGI mode never installs the drain hook, so its send
path is unaffected - asserted separately.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import struct

import httptools

from tests._ws_frames import client_frame as _client_frame
from veloce import Veloce, WebSocket
from veloce.serving.protocol import HttpProtocol


class _FakeTransport(asyncio.Transport):
    """A full-duplex transport that records writes and models a write high mark.

    Not `tests/_protocol.py`'s shared transport: this one drives `pause_writing`
    and `resume_writing` on the protocol, which is the behaviour under test.

    `writelines` and `write` append to `outbound` so frames the handler produces
    are observable. To model a slow-reading peer, the transport invokes the
    protocol's `pause_writing` once the recorded data-frame count reaches
    `high_water` and stays paused until the test calls `drain_outbound()` (which
    fires `resume_writing`) - exactly the contract the event loop drives on a
    real socket whose kernel send buffer fills. This bounds how many frames the
    producer can emit before it must await the drain gate.
    """

    def __init__(self, high_water: int = 8) -> None:
        super().__init__()
        self.outbound: list[bytes] = []
        self._closing = False
        self._high_water = high_water
        self._frames = 0
        self._paused = False
        self._proto: HttpProtocol | None = None

    def bind(self, proto: HttpProtocol) -> None:
        self._proto = proto

    def _record(self, data: bytes) -> None:
        self.outbound.append(bytes(data))
        # A data-frame header's low nibble is its opcode (text 0x1 / binary 0x2).
        if data and (data[0] & 0x0F) in (0x1, 0x2) and (data[0] & 0x80):
            self._frames += 1
            if not self._paused and self._frames >= self._high_water:
                self._paused = True
                if self._proto is not None:
                    self._proto.pause_writing()

    def write(self, data: bytes) -> None:
        self._record(data)

    def writelines(self, frames) -> None:
        for f in frames:
            self._record(f)

    def drain_outbound(self) -> None:
        """Model the kernel draining the send buffer: resume writes."""
        if self._paused:
            self._paused = False
            self._frames = 0
            if self._proto is not None:
                self._proto.resume_writing()

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True

    def get_extra_info(self, name: str, default=None):
        if name == "peername":
            return ("127.0.0.1", 12345)
        return default

    def pause_reading(self) -> None:
        pass

    def resume_reading(self) -> None:
        pass


def _handshake_request(path: str) -> bytes:
    key = base64.b64encode(os.urandom(16)).decode()
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode()


def _feed_upgrade(proto: HttpProtocol, request: bytes) -> None:
    """Feed a handshake to the protocol, swallowing httptools' upgrade signal.

    On a real connection the asyncio transport swallows the `HttpParserUpgrade`
    that httptools raises once the upgrade headers complete; the protocol has
    already diverted into WebSocket mode by then. Calling `data_received`
    directly surfaces that exception, so mirror the transport and suppress it.
    """
    with contextlib.suppress(httptools.HttpParserUpgrade):
        proto.data_received(request)


async def test_slow_consumer_suspends_native_producer():
    app = Veloce(openapi_url=None)
    sent = 0

    @app.websocket("/stream")
    async def stream(ws: WebSocket):
        nonlocal sent
        await ws.accept()
        # Produce indefinitely. With write backpressure, once the transport
        # trips `pause_writing` at its high-water mark the next `send_text`
        # awaits the drain and this loop stalls - it cannot run away.
        while True:
            await ws.send_text(f"msg-{sent}")
            sent += 1

    loop = asyncio.get_running_loop()
    proto = HttpProtocol(app, loop)
    transport = _FakeTransport(high_water=8)
    transport.bind(proto)
    proto.connection_made(transport)
    _feed_upgrade(proto, _handshake_request("/stream"))

    # Let the producer run until the transport's high-water mark trips
    # `pause_writing` and the handler parks on the closed drain gate.
    for _ in range(100):
        await asyncio.sleep(0)
    paused_count = sent

    # The producer is bounded: it stalled at the high-water mark instead of
    # buffering frames without limit. (Without backpressure this loop would run
    # away - the unbounded-write-buffer DoS this guards against.)
    assert paused_count > 0, "handler never produced a frame"
    assert paused_count <= 16, f"producer was not bounded by backpressure: {paused_count}"

    # While the gate stays closed the producer must not advance further.
    for _ in range(100):
        await asyncio.sleep(0)
    assert sent == paused_count, "producer kept sending while the write gate was closed"

    # Modelling the kernel draining the send buffer reopens the gate; the
    # producer resumes from exactly where it stalled - frames stall, never drop.
    transport.drain_outbound()
    for _ in range(50):
        await asyncio.sleep(0)
    assert sent > paused_count, "producer did not resume after the gate reopened"

    # Every produced message reached the transport in order as a real text frame.
    payloads = [
        buf
        for buf in transport.outbound
        if not buf.startswith(b"HTTP/1.1") and buf.startswith(b"msg-")
    ]
    assert payloads[0] == b"msg-0"
    assert payloads == [f"msg-{i}".encode() for i in range(len(payloads))]

    proto.connection_lost(None)


async def test_drain_returns_immediately_when_writable():
    """The drain gate is open by default, so a writable connection never parks."""
    app = Veloce(openapi_url=None)
    loop = asyncio.get_running_loop()
    proto = HttpProtocol(app, loop)
    transport = _FakeTransport()
    proto.connection_made(transport)
    # Writable from connection_made; drain must complete without suspending.
    await asyncio.wait_for(proto.drain(), timeout=1.0)
    proto.connection_lost(None)


async def test_drain_returns_on_closing_transport():
    """A closing transport must not park the producer forever."""
    app = Veloce(openapi_url=None)
    loop = asyncio.get_running_loop()
    proto = HttpProtocol(app, loop)
    transport = _FakeTransport()
    proto.connection_made(transport)
    proto.pause_writing()  # gate closed
    transport.close()  # but the connection is going away
    # Even with the gate closed, a closing transport short-circuits the wait.
    await asyncio.wait_for(proto.drain(), timeout=1.0)
    proto.connection_lost(None)


async def test_asgi_send_path_has_no_drain_hook():
    """ASGI-mode WebSockets never install the native drain hook.

    The ASGI server owns outbound flow control, so `_send_drain` stays None and
    the ASGI send path is byte-for-byte unchanged by the native backpressure.
    """
    sent: list[dict] = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    scope = {"type": "websocket", "path": "/ws", "headers": []}
    ws = WebSocket.from_asgi(scope, receive, send)
    assert ws._send_drain is None
    await ws.accept()
    await ws.send_text("hello")
    assert ws._send_drain is None
    assert sent[-1] == {"type": "websocket.send", "text": "hello"}


def test_websocket_receive_queue_is_capped_by_default():
    """The unbounded queue was a DoS vector. Default is now a finite
    cap; the underlying `asyncio.Queue.maxsize` carries the limit."""

    async def go() -> None:
        ws = WebSocket(transport=None, headers={})  # type: ignore[arg-type]
        assert ws._receive_queue.maxsize == WebSocket.DEFAULT_RECV_QUEUE_MAXSIZE

    asyncio.run(go())


def test_websocket_receive_queue_maxsize_override():
    """Apps with legitimate high-burst peers can raise the cap via the
    ctor — the override is honoured."""

    async def go() -> None:
        ws = WebSocket(transport=None, headers={}, recv_queue_maxsize=4)  # type: ignore[arg-type]
        assert ws._receive_queue.maxsize == 4

    asyncio.run(go())


def test_websocket_queue_full_closes_connection_with_1009():
    """When the receive queue fills up, the next inbound frame must not
    raise `QueueFull` out of `feed_data` (which is a synchronous asyncio
    Protocol callback). Instead the connection is closed with `1009
    Message Too Big` and the transport is shut down — the documented
    backpressure mechanism at this layer."""

    closed: list[bool] = []
    written: list[bytes] = []

    class FakeTransport:
        def write(self, data: bytes) -> None:
            written.append(data)

        def close(self) -> None:
            closed.append(True)

        def is_closing(self) -> bool:
            return bool(closed)

    async def go() -> None:
        ws = WebSocket(
            transport=FakeTransport(),  # type: ignore[arg-type]
            headers={},
            recv_queue_maxsize=2,
        )
        # RFC 6455 Sec. 5.1: a client-to-server frame MUST be masked. An
        # unmasked one is rejected as a protocol error before its length is
        # resolved, so it never reaches the queue this test is about.
        frame = _client_frame(0x1, b"x")
        # Fill the queue, then deliver one more — the third one
        # would have raised QueueFull; instead the WS closes itself.
        ws.feed_data(frame)
        ws.feed_data(frame)
        ws.feed_data(frame)
        assert ws._receive_queue.qsize() == 2, "the queue never filled"
        assert ws._closed is True
        assert closed == [True]
        assert written, "expected a Close frame on the way out"
        assert written[-1][0] & 0x0F == 0x8
        # Close payload: a big-endian 16-bit code. 1009 is Message Too Big.
        assert struct.unpack("!H", written[-1][2:4])[0] == 1009

    asyncio.run(go())


# ── the automatic PONG is the one send that cannot await the drain ────
#
# `_send_frame` for a PONG runs inside the *synchronous* frame parser, so it
# never reached the gate the tests above exercise. A peer that stopped reading
# while flooding PINGs grew the transport's write buffer by one PONG per PING,
# with no cap: 6.55 MB of PINGs queued 6.35 MB of PONGs, `pause_writing` fired
# once and writes simply continued. These drive `WebSocket` directly - no event
# loop needed, because the path under test is synchronous.


class _CountingTransport:
    """Records writes; never applies backpressure of its own."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buf += data

    def writelines(self, parts) -> None:
        for part in parts:
            self.buf += part

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str, default=None):
        return default


def _ping_socket(gate=None) -> tuple[WebSocket, _CountingTransport]:
    transport = _CountingTransport()
    ws = WebSocket(transport, headers={})
    ws._accepted = True
    if gate is not None:
        ws.set_send_gate(gate)
    return ws, transport


def test_a_ping_flood_does_not_queue_a_pong_per_ping_while_writing_is_paused():
    """NEGATIVE: the PONG reply must respect write backpressure.

    RFC 6455 Sec. 5.5.3 lets an endpoint with PONGs outstanding reply to only
    the most recently processed PING, which bounds the reply at one frame.
    """
    ws, transport = _ping_socket(gate=lambda: False)

    for _ in range(100):
        ws.feed_data(_client_frame(0x9, b"hello"))

    assert transport.buf == b""
    assert ws._pending_pong == b"hello"


def test_the_deferred_pong_is_sent_once_writing_resumes():
    """POSITIVE: deferring must not mean dropping - the peer still gets a reply."""
    ws, transport = _ping_socket(gate=lambda: False)
    for _ in range(100):
        ws.feed_data(_client_frame(0x9, b"hello"))

    ws.flush_pending_pong()

    assert transport.buf.startswith(b"\x8a")
    assert ws._pending_pong is None
    # One reply for the whole burst, not one per ping.
    assert transport.buf.count(b"\x8a") == 1


def test_a_pong_is_sent_immediately_when_writing_is_possible():
    """POSITIVE: the ordinary case must be unchanged."""
    ws, transport = _ping_socket(gate=lambda: True)

    ws.feed_data(_client_frame(0x9, b"hello"))

    assert transport.buf.startswith(b"\x8a")


def test_asgi_mode_installs_no_gate_and_replies_as_before():
    """POSITIVE: only the native transport installs the gate.

    Under ASGI the server owns its own backpressure, so the reply must go out
    exactly as it always did.
    """
    ws, transport = _ping_socket()

    ws.feed_data(_client_frame(0x9, b"hello"))

    assert ws._send_gate is None
    assert transport.buf.startswith(b"\x8a")


def test_flushing_with_nothing_deferred_writes_nothing():
    """POSITIVE: `resume_writing` fires often; an idle flush must be free."""
    ws, transport = _ping_socket(gate=lambda: True)

    ws.flush_pending_pong()

    assert transport.buf == b""
