"""Raw-transport framing, heartbeat and frame sending for the built-in server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from typing import TYPE_CHECKING, Any

from veloce.exceptions import WebSocketDisconnect
from veloce.status import (
    WS_1000_NORMAL_CLOSURE,
    WS_1002_PROTOCOL_ERROR,
    WS_1005_NO_STATUS_RCVD,
    WS_1006_ABNORMAL_CLOSURE,
    WS_1007_INVALID_FRAME_PAYLOAD_DATA,
    WS_1009_MESSAGE_TOO_BIG,
)
from veloce.websocket.close import _PEER_CLOSE_CODES_OK
from veloce.websocket.frames import (
    _OP_CLOSE,
    _OP_PING,
    _RAW_DISCONNECT,
    _unmask,
)
from veloce.websocket.utf8 import _Utf8Validator

if TYPE_CHECKING:  # pragma: no cover
    pass


if TYPE_CHECKING:  # pragma: no cover

    class _Host:
        """The `WebSocket` state and members this mixin reads and writes.

        Declared for the type checker only: at runtime the mixin's base is
        `object` and the attributes live in `WebSocket.__slots__`.
        """

        transport: Any
        close_code: int | None
        close_reason: str
        _closed: bool
        _close_frame_sent: bool
        _peer_closed: bool
        _peer_close_event: asyncio.Event | None
        _frag_opcode: int | None
        _frag_buffer: bytearray
        _frag_validator: _Utf8Validator | None
        _recv_buffer: bytearray
        _receive_queue: asyncio.Queue[bytes | object] | None
        _heartbeat: float | None
        _hb_handle: asyncio.TimerHandle | None
        _hb_token: int | None
        _hb_next_token: int
        _hb_saw_inbound: bool
        _send_drain: Any
        _send_gate: Any
        _pending_pong: bytes | None
        _handler_exc: BaseException | None
        CLOSE_HANDSHAKE_TIMEOUT: float
        MAX_FRAME_SIZE: int
        MAX_MESSAGE_SIZE: int

        @property
        def _is_asgi(self) -> bool: ...
        async def close(self, code: int = ..., reason: str = ...) -> None: ...

else:
    _Host = object


_logger = logging.getLogger(__name__)


class RawTransportMixin(_Host):
    """RFC 6455 framing, heartbeat and writes for a `WebSocket` on a raw transport."""

    __slots__ = ()

    # ── Raw-transport framing (RFC 6455 Sec. 5) ───────────────

    def feed_data(self, data: bytes) -> None:
        """Feed raw bytes from the transport (called by the protocol).

        The transport delivers byte runs that need not align with frame boundaries, so
        a partial frame is kept for the next call.

        Fragmented messages (RFC 6455 Sec. 5.4) are reassembled: a data frame with
        `FIN=0` opens a message that continuation frames extend and the `FIN=1`
        continuation completes. Control frames are never fragmented and may be
        interleaved without disturbing the reassembly buffer.
        """
        if not data:
            return
        self._note_heartbeat_inbound()
        self._recv_buffer += data
        # A local offset, compacted once at the end: slicing per frame would
        # memmove the tail repeatedly, making a multi-frame read O(k * n).
        pos = 0
        try:
            while not self._closed:
                consumed = self._parse_frame(pos)
                if consumed == 0:
                    break
                pos += consumed
        finally:
            if pos:
                del self._recv_buffer[:pos]

    def _parse_frame(self, start: int = 0) -> int:
        """Parse one whole frame from `_recv_buffer` beginning at `start`.

        Returns the number of bytes from `start` the frame occupied, or `0` when the
        buffer does not yet hold a complete frame, in which case the caller keeps
        them for the next `feed_data`. Reading relative to `start` is what lets
        `feed_data` walk several frames without re-slicing per frame.
        """
        buf = self._recv_buffer
        n = len(buf) - start
        if n < 2:
            return 0

        fin = bool(buf[start] & 0x80)
        opcode = buf[start] & 0x0F
        masked = bool(buf[start + 1] & 0x80)
        payload_len = buf[start + 1] & 0x7F

        # RFC 6455 Sec. 5.2: RSV1-3 (0x70) MUST be zero without a defining
        # extension, and Veloce negotiates none. Rejected before allocation.
        if buf[start] & 0x70:
            self._close_protocol_error()
            return 0

        # RFC 6455 Sec. 5.1: a client-to-server frame MUST be masked; an unmasked
        # one fails the connection with 1002.
        if not masked:
            self._close_protocol_error()
            return 0

        offset = 2
        if payload_len == 126:
            if n < 4:
                return 0
            payload_len = struct.unpack("!H", buf[start + 2 : start + 4])[0]
            offset = 4
        elif payload_len == 127:
            if n < 10:
                return 0
            payload_len = struct.unpack("!Q", buf[start + 2 : start + 10])[0]
            offset = 10

        # Before allocating: a huge declared length must not park unbounded bytes.
        if payload_len > self.MAX_FRAME_SIZE:
            self._close_too_big()
            return 0

        # RFC 6455 Sec. 5.5: <=125 bytes and never fragmented, so a violation is
        # 1002 rather than an oversized ping echoed back as a pong.
        if opcode in (0x8, 0x9, 0xA) and (payload_len > 125 or not fin):
            self._close_protocol_error()
            return 0

        # Unconditional: every unmasked frame returned above, so re-testing
        # `masked` would leave two always-true branches on the per-frame path.
        if n < offset + 4:
            return 0
        mask = buf[start + offset : start + offset + 4]
        offset += 4

        frame_len = offset + payload_len
        if n < frame_len:
            return 0

        payload = bytes(buf[start + offset : start + offset + payload_len])
        if payload_len:
            payload = _unmask(payload, mask, payload_len)

        if opcode == 0x8:  # _OP_CLOSE
            # Peer-initiated close (RFC 6455 Sec. 5.5.1); `_handle_close_frame`
            # replies, so a clean-exit `close()` emits no second frame.
            self._peer_closed = True
            if self._peer_close_event is not None:
                self._peer_close_event.set()
            self._handle_close_frame(payload)
            raise WebSocketDisconnect(self.close_code or WS_1000_NORMAL_CLOSURE)
        if opcode == 0x9:  # _OP_PING
            gate = self._send_gate
            if gate is None or gate():
                self._send_frame(payload, opcode=0xA)  # _OP_PONG
            else:
                # RFC 6455 Sec. 5.5.3 permits replying to only the most recent
                # PING; a peer flooding PINGs while not reading would otherwise
                # grow the un-awaitable write buffer by a PONG apiece.
                self._pending_pong = payload
            return frame_len
        if opcode == 0xA:  # _OP_PONG
            # Confirms the peer answered this window's probe, so the next issues a PING.
            if self._hb_token is not None and payload == self._hb_token.to_bytes(4, "big"):
                self._hb_token = None
            return frame_len

        if opcode in (0x1, 0x2):  # _OP_TEXT / _OP_BINARY
            # RFC 6455 Sec. 5.4 allows only continuations after the opening frame.
            if self._frag_opcode is not None:
                self._close_protocol_error()
                return 0
            if opcode == 0x1:  # _OP_TEXT
                # RFC 6455 Sec. 8.1: TEXT must be UTF-8, and trip here, not at decode.
                validator = _Utf8Validator()
                if not validator.feed(payload):
                    self._close_invalid_payload()
                    return 0
            else:
                validator = None
            if fin:
                # Unfragmented: a TEXT frame must end on a codepoint boundary.
                if validator is not None and not validator.done:
                    self._close_invalid_payload()
                    return 0
                self._frag_opcode = None
                self._frag_buffer = bytearray()
                self._frag_validator = None
                self._enqueue_or_close(payload)
            else:
                # Opening frame: supersedes any abandoned partial.
                self._frag_opcode = opcode
                self._frag_buffer = bytearray(payload)
                self._frag_validator = validator
                if len(self._frag_buffer) > self.MAX_MESSAGE_SIZE:
                    self._close_too_big()
                    return 0
        elif opcode == 0x0:  # _OP_CONT
            if self._frag_opcode is None:
                # RFC 6455 Sec. 5.4: a continuation with no message is a 1002.
                self._close_protocol_error()
                return 0
            # Against the in-progress message's UTF-8 state; None if binary.
            if self._frag_validator is not None and not self._frag_validator.feed(payload):
                self._close_invalid_payload()
                return 0
            self._frag_buffer += payload
            # The per-frame cap bounds one frame, not a stream of continuations.
            if len(self._frag_buffer) > self.MAX_MESSAGE_SIZE:
                self._close_too_big()
                return 0
            if fin:
                # Final fragment: TEXT must end on a codepoint boundary.
                if self._frag_validator is not None and not self._frag_validator.done:
                    self._close_invalid_payload()
                    return 0
                self._enqueue_or_close(bytes(self._frag_buffer))
                self._frag_opcode = None
                self._frag_buffer = bytearray()
                self._frag_validator = None

        return frame_len

    def _terminate_raw(self, code: int, *, record_close_code: bool) -> None:
        """Send a close frame carrying `code` synchronously, then drop the transport.

        The single raw-mode teardown core, synchronous because no `await` is
        available from inside the Protocol callback that drives `feed_data`.
        `record_close_code` writes `code` into `close_code` for a locally-initiated
        close; the peer-reply path leaves that to `_handle_close_frame`, which
        records the peer's own code.
        """
        with contextlib.suppress(Exception):
            self._send_frame(code.to_bytes(2, "big"), opcode=_OP_CLOSE)
        self._cancel_heartbeat()
        if record_close_code:
            self.close_code = code
        self._closed = True
        self._close_frame_sent = True
        self._wake_raw_receiver()
        with contextlib.suppress(Exception):
            if self.transport is not None:
                self.transport.close()

    def _close_control(self, code: int) -> None:
        """Locally close the connection with `code` (records `close_code`).

        Shared by the parser-side close paths (`_close_too_big`,
        `_close_protocol_error`, `_close_invalid_payload`).
        """
        self._terminate_raw(code, record_close_code=True)

    def _close_too_big(self) -> None:
        """Close the connection with `1009 Message Too Big`.

        Used when a peer declares a frame payload past `MAX_FRAME_SIZE`.
        """
        self._close_control(WS_1009_MESSAGE_TOO_BIG)

    def _close_protocol_error(self) -> None:
        """Close the connection with `1002 Protocol Error`.

        Used for malformed frames - e.g. an oversized (>125 byte) or
        fragmented control frame (RFC 6455 Sec. 5.5).
        """
        self._close_control(WS_1002_PROTOCOL_ERROR)

    def _close_invalid_payload(self) -> None:
        """Close the connection with `1007 Invalid Frame Payload Data`.

        Used when a TEXT message (whole or reassembled from fragments) is
        not valid UTF-8 (RFC 6455 Sec. 8.1).
        """
        self._close_control(WS_1007_INVALID_FRAME_PAYLOAD_DATA)

    def _handle_close_frame(self, payload: bytes | bytearray) -> None:
        """Process a received Close frame: validate, echo, and record state.

        Per RFC 6455 Sec. 5.5.1 a Close payload is either empty or a 2-byte
        big-endian status code optionally followed by a UTF-8 reason. An empty
        payload means "no status", recorded as the reserved 1005 without putting it
        on the wire. A 1-byte payload, a code the peer may not send, or a non-UTF-8
        reason is a protocol error answered with 1002.
        """
        n = len(payload)
        if n == 0:
            self.close_code = WS_1005_NO_STATUS_RCVD
            self._echo_close(WS_1000_NORMAL_CLOSURE)
            return
        if n == 1:
            self._close_protocol_error()
            return
        code = struct.unpack("!H", payload[:2])[0]
        # RFC 6455 Sec. 7.4.2: a registered code, or a private one in 3000-4999.
        if code > 4999 or (code < 3000 and code not in _PEER_CLOSE_CODES_OK):
            self._close_protocol_error()
            return
        try:
            reason = payload[2:].decode("utf-8") if n > 2 else ""
        except UnicodeDecodeError:
            self._close_invalid_payload()
            return
        self.close_code = code
        self.close_reason = reason
        # RFC 6455 Sec. 5.5.1: the reply Close SHOULD echo the peer's status code.
        self._echo_close(code)

    def _echo_close(self, code: int) -> None:
        """Send a Close frame in reply to a peer close and tear down.

        The reply carries `code` (a 2-byte status) and no reason. `close_code`
        is left untouched - `_handle_close_frame` already recorded the peer's
        own status before echoing. Both the frame write and the transport close
        are best-effort - the peer may already be gone.
        """
        self._terminate_raw(code, record_close_code=False)

    def _enqueue_or_close(self, payload: bytes) -> None:
        """Push a reassembled message onto the receive queue.

        `QueueFull` is the backpressure signal at this layer, answered with `1009
        Message Too Big` rather than letting the exception unwind into the asyncio
        Protocol callback or growing the queue without bound.
        """
        queue = self._receive_queue
        assert queue is not None  # raw transport only; the ASGI path never reaches here
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Synchronous: no `await` is available from inside `feed_data`.
            self._close_too_big()

    # ── Heartbeat (raw-transport liveness) ────────────────────
    #
    # A peer gone without FIN/RST would park a handler in `receive_*` forever, so
    # a tokened PING per window must draw its PONG, and two silent windows mark
    # the peer dead. Inert under ASGI.

    def _init_heartbeat(self, heartbeat: float | None) -> None:
        """Initialise heartbeat state; called from every constructor path."""
        self._heartbeat = heartbeat
        # `None` when no heartbeat is armed.
        self._hb_handle = None
        # `None` when no PING is awaiting a PONG; a PONG echoing it proves life.
        self._hb_token = None
        self._hb_next_token = 0
        # Set by `feed_data`, so the next tick skips the dead-peer teardown.
        self._hb_saw_inbound = False

    def start_heartbeat(self) -> None:
        """Arm the heartbeat timer for a raw-transport connection.

        Idempotent, and a no-op in ASGI mode or without a configured `heartbeat`.
        `accept()` calls it once the raw handshake completes; it is public so a
        handler that builds a `WebSocket` by hand can start the probe itself.
        """
        if self._heartbeat is None or self._is_asgi or self._closed:
            return
        if self._hb_handle is not None:
            return
        self._schedule_heartbeat()

    def _schedule_heartbeat(self) -> None:
        """Schedule the next heartbeat tick `heartbeat` seconds out.

        Only reached with a configured (non-`None`) heartbeat - callers gate
        on `self._heartbeat is None` first.
        """
        interval = self._heartbeat
        assert interval is not None
        # A running loop is always present: both call sites are inside it.
        loop = asyncio.get_running_loop()
        self._hb_handle = loop.call_later(interval, self._heartbeat_tick)

    def _heartbeat_tick(self) -> None:
        """Run one heartbeat window: detect a dead peer or send a probe.

        Inbound traffic since the last tick proves the peer alive; an outstanding PING
        with none means it is silent, and the connection goes with 1006.
        """
        self._hb_handle = None
        if self._closed:
            return
        if self._hb_saw_inbound:
            self._hb_saw_inbound = False
            self._hb_token = None
            self._schedule_heartbeat()
            return
        if self._hb_token is not None:
            # The previous window's PING went unanswered and nothing else arrived.
            self._close_heartbeat_timeout()
            return
        token = self._hb_next_token
        self._hb_next_token = (token + 1) & 0xFFFFFFFF
        self._hb_token = token
        with contextlib.suppress(Exception):
            self._send_frame(token.to_bytes(4, "big"), opcode=_OP_PING)
        self._schedule_heartbeat()

    def _note_heartbeat_inbound(self) -> None:
        """Record that inbound bytes arrived (called from `feed_data`).

        Coalesced to a single flag rather than re-arming the timer per byte, and
        consulted lazily when it next fires.
        """
        if self._heartbeat is not None and self._hb_handle is not None:
            self._hb_saw_inbound = True

    def _cancel_heartbeat(self) -> None:
        """Cancel the heartbeat timer (called from every close path)."""
        if self._hb_handle is not None:
            self._hb_handle.cancel()
            self._hb_handle = None
        self._hb_token = None
        self._hb_saw_inbound = False

    def _close_heartbeat_timeout(self) -> None:
        """Tear down a connection whose peer stopped answering heartbeats.

        1006 is a reserved code that must never appear on the wire
        (RFC 6455 Sec. 7.4.1), and the peer is presumed gone, so no Close
        frame is sent - the transport is simply dropped and `close_code` is
        recorded as 1006 for the handler to observe.
        """
        self._cancel_heartbeat()
        self._closed = True
        self.close_code = WS_1006_ABNORMAL_CLOSURE
        self._wake_raw_receiver()
        with contextlib.suppress(Exception):
            if self.transport is not None:
                self.transport.close()

    def _wake_raw_receiver(self) -> None:
        """Wake a handler parked in `receive_*()` on a terminal raw close.

        A parser-side close sets `_closed` and drops the transport, but a coroutine
        already blocked on the receive queue would hang until its own timeout, so the
        terminal sentinel is delivered to unwind it. Raw-transport mode only.
        """
        if self._receive_queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._receive_queue.put_nowait(_RAW_DISCONNECT)

    async def _raw_send(self, data: bytes, opcode: int) -> None:
        """Write one frame on the raw transport, honouring write backpressure.

        Drains BEFORE writing, so a slow-reading client suspends the producing handler
        here instead of letting `_send_frame` buffer unbounded bytes. `_send_frame`
        itself stays synchronous, because the sync Protocol callback that drives
        `feed_data` also invokes it for pong and close replies.
        """
        if self._send_drain is not None:
            await self._send_drain()
        self._send_frame(data, opcode=opcode)

    def _send_frame(self, data: bytes, opcode: int) -> None:
        """Send a WebSocket frame.

        `writelines` avoids a `bytearray.extend` copy of the potentially KiB-sized
        payload followed by a `bytes(frame)` copy on the way out.
        """
        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode

        length = len(data)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))

        # `writelines` keeps header and payload as separate buffers; the fallback
        # is for transports implementing only the basic `WriteTransport` API.
        writelines = getattr(self.transport, "writelines", None)
        if writelines is not None:
            writelines((bytes(header), data))
        else:
            self.transport.write(bytes(header) + bytes(data))
