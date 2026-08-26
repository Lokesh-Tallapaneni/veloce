"""WebSocket support — basic implementation over raw asyncio."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import enum
import hashlib
import inspect
import logging
import math
import struct
from typing import TYPE_CHECKING, Any, NoReturn, cast

import orjson

from veloce._constants import (
    HEADER_COOKIE,
    HEADER_ORIGIN,
    HEADER_SEC_WEBSOCKET_KEY,
    HEADER_SEC_WEBSOCKET_PROTOCOL,
)
from veloce._internal import _is_async_callable, _reject_header_crlf, dumps_for, offload
from veloce._protocol_constants import (
    ASGI_EVENT_WS_ACCEPT,
    ASGI_EVENT_WS_CLOSE,
    ASGI_EVENT_WS_CONNECT,
    ASGI_EVENT_WS_DISCONNECT,
    ASGI_EVENT_WS_SEND,
)
from veloce.exceptions import WebSocketDisconnect
from veloce.http.cookies import parse_cookie
from veloce.http.datastructures import Address, QueryParams, State
from veloce.status import (
    WS_1000_NORMAL_CLOSURE,
    WS_1001_GOING_AWAY,
    WS_1002_PROTOCOL_ERROR,
    WS_1005_NO_STATUS_RCVD,
    WS_1006_ABNORMAL_CLOSURE,
    WS_1007_INVALID_FRAME_PAYLOAD_DATA,
    WS_1009_MESSAGE_TOO_BIG,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable, Callable, Coroutine, Iterable


# RFC 6455 Sec. 1.3: the server's `Sec-WebSocket-Accept` is the base64 of the
# SHA-1 of the client's `Sec-WebSocket-Key` concatenated with this fixed GUID.
_WS_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def compute_accept(key: str) -> str:
    """Compute the RFC 6455 Sec. 1.3 `Sec-WebSocket-Accept` value for `key`.

    The single source of the handshake-accept math: both the raw-transport
    `WebSocket.accept` branch and the native `HttpProtocol` upgrade handler call
    this so the 101 response carries exactly one, consistent accept key. SHA-1 is
    mandated by the spec for the handshake fingerprint - it is not used here as a
    security primitive.
    """
    return base64.b64encode(
        hashlib.sha1((key + _WS_ACCEPT_GUID).encode()).digest()  # noqa: S324
    ).decode()


def _validate_positive_seconds(value: float | None, name: str) -> None:
    """Reject a non-finite or non-positive duration in seconds.

    Mirrors `EventSourceResponse.ping` validation: `None` disables the
    feature, otherwise the value must be a finite positive number of
    seconds. NaN fails `> 0` and Infinity passes `> 0` but is meaningless
    as a deadline, so both are rejected via `math.isfinite`.
    """
    if value is not None and not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a finite positive number of seconds, got {value!r}")


def _validate_idle_timeout(idle_timeout: float | None) -> None:
    """Reject a non-finite or non-positive idle timeout."""
    _validate_positive_seconds(idle_timeout, "idle_timeout")


def _validate_heartbeat(heartbeat: float | None) -> None:
    """Reject a non-finite or non-positive heartbeat interval."""
    _validate_positive_seconds(heartbeat, "heartbeat")


# Close codes a peer is permitted to send in a Close frame body (RFC 6455
# Sec. 7.4). 1005/1006/1015 are reserved status codes the protocol never
# puts on the wire, and 1004 is undefined; receiving any of them - or any
# code below 1000 or an unassigned code in the 1016-2999 range - is a
# protocol error answered with 1002. Codes >=3000 are application/registry
# defined and accepted without a registry check.
_PEER_CLOSE_CODES_OK = frozenset(
    {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014}
)

_logger = logging.getLogger(__name__)


#: Bytes unmasked per bignum XOR. Python's big-integer XOR is superlinear in
#: operand size, so one XOR over a whole frame is both slower and far more
#: allocation-hungry than the same work in fixed blocks. Measured on the
#: project's benchmark host, masked-frame unmask, min-of-7:
#:
#:   frame   whole-frame   16 KiB blocks
#:    16 KiB    28.4 us       28.4 us   (identical - below the threshold)
#:    32 KiB    74.3 us       61.0 us   -17.9%
#:    64 KiB   199.7 us      117.1 us   -41.4%
#:     4 MiB    12.1 ms        5.2 ms   -56.7%, peak 4.20x -> 2.00x frame
#:    16 MiB    50.3 ms       22.7 ms   -55.0%, peak 70.5 MB -> 33.6 MB
#:
#: The memory bound is the point: `MAX_FRAME_SIZE` is 16 MiB, so a single
#: masked frame could put ~70 MB of transient bignums on the heap. Block sizes
#: from 8 to 128 KiB all measured within ~1% of each other; 16 KiB is the middle
#: of that plateau.
_UNMASK_BLOCK = 16 * 1024


def _unmask(payload: bytes, mask: bytes | bytearray, length: int) -> bytes:
    """XOR `payload` with the repeating 4-byte `mask` (RFC 6455 Sec. 5.3)."""
    mask = bytes(mask)
    if length <= _UNMASK_BLOCK:
        tiled = (mask * ((length + 3) // 4))[:length]
        return (int.from_bytes(payload, "big") ^ int.from_bytes(tiled, "big")).to_bytes(
            length, "big"
        )
    # One tiled mask integer, reused for every full block; only the ragged tail
    # needs its own.
    block_mask = int.from_bytes(mask * (_UNMASK_BLOCK // 4), "big")
    out = bytearray(length)
    view = memoryview(payload)
    pos = 0
    while True:
        end = pos + _UNMASK_BLOCK
        if end >= length:
            tail = length - pos
            tiled = (mask * ((tail + 3) // 4))[:tail]
            out[pos:length] = (
                int.from_bytes(view[pos:length], "big") ^ int.from_bytes(tiled, "big")
            ).to_bytes(tail, "big")
            return bytes(out)
        out[pos:end] = (int.from_bytes(view[pos:end], "big") ^ block_mask).to_bytes(
            _UNMASK_BLOCK, "big"
        )
        pos = end


def _sanitise_close(code: int, reason: str) -> tuple[int, str]:
    """Normalise an outbound close code and reason to what may go on the wire.

    RFC 6455 Sec. 5.5 caps a control frame at 125 bytes, so the reason is at
    most 123 after the 2-byte code; the truncation walks back to a codepoint
    boundary so the frame stays valid UTF-8. Sec. 7.4.1 reserves 1005, 1006 and
    1015 for local use and forbids them - and anything outside the assigned
    ranges - from appearing on the wire.

    Applied above the transport branch in `close`, because only the raw branch
    clamped: the same call closed cleanly on one transport and, under an ASGI
    server whose library rejects the frame, dropped the socket so the peer saw
    an abnormal 1006 instead.

    An out-of-range code is coerced rather than raised. `close` runs on the
    teardown path, where `Veloce._run_websocket` suppresses exceptions - a raise
    would skip the close entirely and turn a bad code into the 1006 this is
    meant to avoid.
    """
    if code > 4999 or (code < 3000 and code not in _PEER_CLOSE_CODES_OK):
        _logger.warning(
            "WebSocket close code %s may not appear on the wire (RFC 6455 Sec. 7.4.1); "
            "closing with %s instead",
            code,
            WS_1000_NORMAL_CLOSURE,
        )
        code = WS_1000_NORMAL_CLOSURE
    if not reason:
        return code, ""
    encoded = reason.encode("utf-8")
    if len(encoded) <= 123:
        return code, reason
    encoded = encoded[:123]
    while encoded:
        try:
            return code, encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return code, ""


# Terminal sentinel pushed onto the raw receive queue to wake a handler parked
# in `receive_*()` when the connection is closed out-of-band (e.g. a heartbeat
# timeout on a silent peer). Typed `Any` so it can ride the `Queue[bytes]`; the
# receive path checks identity and raises `WebSocketDisconnect`.
_RAW_DISCONNECT: Any = object()


class _Utf8Validator:
    """Incremental UTF-8 validator for streamed TEXT payloads.

    RFC 6455 Sec. 8.1 requires TEXT message payloads to be valid UTF-8 and
    Sec. 5.6 lets a message arrive as several continuation fragments. Rather
    than buffer the whole message and decode once at the end, this consumes
    each fragment as it lands and remembers the partial multi-byte sequence
    that straddles a fragment boundary, so a bad byte is rejected on the
    first offending fragment.

    The decoder is a compact form of the Unicode UTF-8 acceptance automaton:
    `_need` counts the continuation bytes still expected for the codepoint in
    progress, and `_lo`/`_hi` bound the immediately-next byte so overlong
    encodings, surrogate halves (U+D800-U+DFFF) and values past U+10FFFF are
    rejected without decoding to an `str`. `feed` returns False on the first
    invalid byte; `done` is True only at a codepoint boundary.
    """

    __slots__ = ("_need", "_lo", "_hi")

    def __init__(self) -> None:
        self._need = 0
        self._lo = 0x80
        self._hi = 0xBF

    @property
    def done(self) -> bool:
        return self._need == 0

    def feed(self, chunk: bytes | bytearray) -> bool:
        need = self._need
        lo = self._lo
        hi = self._hi
        for byte in chunk:
            if need == 0:
                if byte < 0x80:
                    continue
                if 0xC2 <= byte <= 0xDF:
                    need, lo, hi = 1, 0x80, 0xBF
                elif byte == 0xE0:
                    need, lo, hi = 2, 0xA0, 0xBF
                elif 0xE1 <= byte <= 0xEC or byte in (0xEE, 0xEF):
                    need, lo, hi = 2, 0x80, 0xBF
                elif byte == 0xED:
                    need, lo, hi = 2, 0x80, 0x9F
                elif byte == 0xF0:
                    need, lo, hi = 3, 0x90, 0xBF
                elif 0xF1 <= byte <= 0xF3:
                    need, lo, hi = 3, 0x80, 0xBF
                elif byte == 0xF4:
                    need, lo, hi = 3, 0x80, 0x8F
                else:
                    return False
            else:
                if byte < lo or byte > hi:
                    return False
                need -= 1
                lo, hi = 0x80, 0xBF
        self._need = need
        self._lo = lo
        self._hi = hi
        return True


class WebSocketState(enum.IntEnum):
    """Connection-state enum - ASGI shape.

    `CONNECTING` is the initial state before `accept()` has been sent
    (client side) or received (application side). `CONNECTED` once the
    handshake completes; `DISCONNECTED` once a close frame has been
    sent or received on the corresponding side.
    """

    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2


class WebSocket:
    """WebSocket connection handler.

    Usage::

        from veloce import Veloce, WebSocket

        app = Veloce()

        @app.websocket("/ws")
        async def chat(ws: WebSocket):
            async with ws:
                await ws.accept()
                async for message in ws.iter_text():
                    await ws.send_text(message)

    Using ``async with ws:`` closes the connection on a clean exit with a
    normal-closure 1000. If the block exits via an exception, ``__aexit__``
    leaves the close to the dispatcher's error handling, which sends the
    mapped close code (e.g. 1008 for a policy violation, 1011 for an
    unhandled error) before the exception propagates.

    Pass ``idle_timeout=<seconds>`` (default ``None`` -> disabled) to bound
    how long a blocking receive (``receive``/``receive_text``/
    ``receive_bytes``/``receive_json`` and the ``iter_*`` loops) waits for
    the next message. When no message arrives within ``idle_timeout``
    seconds the connection performs a clean RFC 6455 close with
    ``1001 Going Away`` and the receive raises ``WebSocketDisconnect``, so
    the handler loop unwinds exactly as it would on a peer-initiated close.
    A per-call ``timeout`` still applies; whichever deadline is smaller
    wins. Set it at construction via ``from_asgi(idle_timeout=...)`` or from
    inside the handler with ``set_idle_timeout``. The window bounds each
    complete message (in production, ASGI delivers complete messages and
    owns ping/pong; the raw-transport path measures it the same way).

    Pass ``heartbeat=<seconds>`` (raw-transport mode only, default ``None``
    -> disabled) to proactively probe a silent peer. After ``accept()`` a
    timer sends an application PING carrying a token every ``heartbeat``
    seconds; the peer must answer with a PONG (or send any other frame)
    before the next tick, otherwise the connection is dropped with a
    ``1006`` close code recorded on ``ws.close_code``. Any inbound byte
    defers the next probe, so busy connections send no needless pings. In
    ASGI mode the server owns ping/pong, so the value is accepted for API
    symmetry but never starts a timer.
    """

    # A server holds one instance per live connection for the connection's whole
    # lifetime, so the per-instance `__dict__` is the dominant fixed cost at high
    # concurrency. Both constructors converge on exactly this attribute set, so
    # slotting is complete; `state` remains the namespace for application data.
    __slots__ = (
        "_accepted",
        "_asgi_receive",
        "_asgi_send",
        "_close_frame_sent",
        "_closed",
        "_cookies",
        "_frag_buffer",
        "_frag_opcode",
        "_frag_validator",
        "_handler_exc",
        "_handshake_sent",
        "_hb_handle",
        "_hb_next_token",
        "_hb_saw_inbound",
        "_hb_token",
        "_heartbeat",
        "_idle_timeout",
        "_peer_close_event",
        "_peer_closed",
        "_query_params",
        "_receive_queue",
        "_recv_buffer",
        "_send_drain",
        "_state",
        "accepted_subprotocol",
        "app",
        "close_code",
        "close_reason",
        "headers",
        "path",
        "path_params",
        "scope",
        "transport",
    )

    # RFC 6455 Sec. 1.3 magic GUID, concatenated with the client's
    # `Sec-WebSocket-Key` and SHA-1+base64'd to form `Sec-WebSocket-Accept`.
    # The standalone `compute_accept` helper / `_WS_ACCEPT_GUID` carry the same
    # value; this class attribute is kept for backward compatibility.
    GUID = _WS_ACCEPT_GUID

    # Cap inbound frame backlog. An unbounded `asyncio.Queue` lets a peer
    # that sends faster than the handler reads grow it without limit -
    # a DoS / backpressure vector. With a cap, the protocol's `put` (the
    # producer) `await`s once the cap is hit, which is the backpressure
    # signal: the application either reads faster or closes the
    # connection. Configurable via constructor for apps with legitimate
    # high-burst peers.
    DEFAULT_RECV_QUEUE_MAXSIZE = 64

    # Upper bound on a single frame's declared payload length. A peer can
    # declare an 8-byte (64-bit) length in the frame header; without a cap
    # the parser would wait for - and the reassembly buffer would try to
    # hold - an arbitrarily large amount of data, an unbounded-allocation
    # DoS. Frames declaring a payload larger than this close the connection
    # with `1009 Message Too Big`.
    MAX_FRAME_SIZE = 16 * 1024 * 1024

    # Upper bound on a reassembled (fragmented) message's total size. The
    # per-frame cap bounds one frame, but a peer can open a fragmented
    # message (FIN=0) and stream an unbounded number of continuation
    # frames - each individually under MAX_FRAME_SIZE - to grow the
    # reassembly buffer without limit. Crossing this bound closes the
    # connection with `1009 Message Too Big`.
    MAX_MESSAGE_SIZE = 16 * 1024 * 1024

    # Heartbeat state (raw-transport liveness). Declared here so the type is
    # known to readers like `_parse_frame`'s PONG branch that run before the
    # `_init_heartbeat` assignments are seen; populated by `_init_heartbeat`.
    _heartbeat: float | None
    _hb_handle: asyncio.TimerHandle | None
    _hb_token: int | None
    _hb_next_token: int
    _hb_saw_inbound: bool

    # Bound on how long a server-initiated raw-transport `close()` waits for the
    # peer's reply close frame before dropping the TCP connection (RFC 6455
    # Sec. 7.1.1: an endpoint may close the connection if the peer's close does
    # not arrive within a reasonable time). Keeps a well-behaved peer's clean
    # handshake intact while never blocking shutdown on an unresponsive peer.
    CLOSE_HANDSHAKE_TIMEOUT = 5.0

    def __init__(
        self,
        transport: asyncio.Transport,
        headers: dict[str, str],
        recv_queue_maxsize: int | None = None,
        idle_timeout: float | None = None,
        heartbeat: float | None = None,
    ) -> None:
        _validate_idle_timeout(idle_timeout)
        _validate_heartbeat(heartbeat)
        self.transport = transport
        self.headers = headers
        self._idle_timeout = idle_timeout
        self._init_common()
        self._init_heartbeat(heartbeat)
        maxsize = (
            recv_queue_maxsize
            if recv_queue_maxsize is not None
            else self.DEFAULT_RECV_QUEUE_MAXSIZE
        )
        # Carries reassembled `bytes` messages plus the `_RAW_DISCONNECT`
        # sentinel the frame parser enqueues to wake a parked receiver on close.
        self._receive_queue: asyncio.Queue[bytes | object] = asyncio.Queue(maxsize=maxsize)

    def _init_common(self) -> None:
        """Set every mode-independent default field shared by both constructors.

        Called by `__init__` (raw-transport) and `from_asgi` (which builds the
        instance via `cls.__new__` to skip the transport-required `__init__`).
        Centralising these assignments means a newly added field is initialised
        on both paths from one place, rather than silently surfacing as an
        ASGI-only `AttributeError`. The per-mode fields stay in the callers:
        `transport`/`headers`/`_idle_timeout` (whose static types differ between
        the two paths), the receive queue, the heartbeat arg, the ASGI
        send/receive callables, and the scope/path that `from_asgi` and
        `from_transport` fill in.
        """
        # The owning application, stamped by `Veloce._run_websocket` for both
        # transports, so a handler reaches it the way `Request.app` does. ASGI's
        # `scope["app"]` is not populated, so this is the supported accessor
        # alongside the `current_app` proxy.
        self.app: Any = None
        # Backs the lazily-built `state` namespace; a declared slot starts unset,
        # so the sentinel is assigned here to keep the property a plain read.
        self._state: State | None = None
        self._accepted = False
        self._closed = False
        # Peer-initiated close tracking for the raw-transport close handshake
        # (RFC 6455 Sec. 5.5.1). `_peer_closed` records that the peer sent the
        # first close frame, so the server's `close()` can skip waiting for a
        # reply it has already received. The peer's status code is validated and
        # recorded into `close_code` by `_handle_close_frame`.
        self._peer_closed = False
        # Distinct from `_closed`: a peer-initiated close sets `_closed` from
        # inside the frame parser before the server has sent its own (reply)
        # close frame. `close()` keys the actual frame write off this flag so a
        # peer-driven close still triggers the server's reply, completing the
        # RFC 6455 Sec. 5.5.1 handshake instead of returning early on `_closed`.
        self._close_frame_sent = False
        # Signalled by the frame parser when the peer's close frame arrives, so a
        # server-initiated `close()` can await the reply (bounded by
        # `CLOSE_HANDSHAKE_TIMEOUT`) before dropping the transport. Created lazily
        # by `close()` since the common ASGI path never uses it.
        self._peer_close_event: asyncio.Event | None = None
        # True only on the native upgrade path, where `HttpProtocol` already
        # wrote the 101 synchronously to switch the byte stream. `accept()` must
        # then NOT emit a second handshake. Default False so direct construction
        # and `from_asgi` keep writing the 101 themselves.
        self._handshake_sent = False
        # Fragmented-message reassembly state (RFC 6455 Sec. 5.4). `_frag_opcode`
        # is the data opcode of the message currently being assembled, or
        # `None` when no fragmented message is in progress.
        self._frag_opcode: int | None = None
        self._frag_buffer: bytearray = bytearray()
        # Incremental UTF-8 validator for the TEXT message currently being
        # reassembled (`None` for a binary message); see `_Utf8Validator`.
        self._frag_validator: _Utf8Validator | None = None
        # The handler's exception, kept so a driver can still report it when the
        # close handshake it was raised through is cancelled by a vanished peer.
        self._handler_exc: BaseException | None = None
        # Peer-supplied close code/reason, populated when a Close frame is
        # received (raw-transport mode). `close_code` stays `None` until the
        # peer closes; `close_reason` is the decoded UTF-8 reason or "".
        self.close_code: int | None = None
        self.close_reason: str = ""
        # The subprotocol this connection settled on, recorded by `accept` so a
        # handler can read back what it negotiated instead of tracking it
        # itself. `None` until accepted, and when no subprotocol was chosen.
        self.accepted_subprotocol: str | None = None
        # Persistent receive buffer for incremental frame parsing. The
        # transport hands `feed_data` arbitrary byte runs that do not line up
        # with frame boundaries - bytes accumulate here until a whole frame
        # (or several) can be parsed off the front.
        self._recv_buffer: bytearray = bytearray()
        # ASGI mode (W1). When wired through `Veloce.__call__`'s websocket
        # branch, the transport is None and we drive the connection through
        # ASGI receive/send callables instead. Set by `from_asgi`.
        self._asgi_receive: Any = None
        self._asgi_send: Any = None
        self.scope: dict[str, Any] | None = None
        self.path: str = ""
        self.path_params: dict[str, Any] = {}
        self._query_params: Any = None
        self._cookies: dict[str, str] | None = None
        # Write-side backpressure hook for the native raw-transport path. The
        # protocol installs an awaitable (`HttpProtocol.drain`) via
        # `set_send_drain`; the async send wrappers await it before each frame so
        # a slow-reading client suspends the producer instead of growing the
        # transport write buffer without bound. Stays `None` in ASGI mode (the
        # ASGI server owns flow control) and on direct construction, so the
        # send path pays a single `is not None` check.
        self._send_drain: Any = None

    @classmethod
    def from_asgi(
        cls,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        idle_timeout: float | None = None,
        heartbeat: float | None = None,
    ) -> WebSocket:
        """Construct an ASGI-driven WebSocket (no asyncio.Transport).

        Used by `Veloce.__call__` for `scope["type"] == "websocket"`.
        Headers come from `scope["headers"]` (list of `(bytes, bytes)`),
        decoded latin-1 per ASGI. `accept`/`send_*`/`receive_*`/`close`
        all dispatch through `send`/`receive` instead of the raw frame
        writer used by the asyncio.Transport mode.

        `idle_timeout` (default `None` -> disabled) bounds how long a
        blocking receive waits for the next frame before performing a
        clean `1001 Going Away` close; see the class docstring. `heartbeat`
        is accepted for signature symmetry with the raw-transport
        constructor but is inert here - the ASGI server owns ping/pong.
        """
        _validate_idle_timeout(idle_timeout)
        _validate_heartbeat(heartbeat)
        headers: dict[str, str] = {}
        for k, v in scope.get("headers", []):
            headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        # `cls.__new__` to skip the transport-required __init__.
        ws = cls.__new__(cls)
        ws.transport = None  # type: ignore[assignment]
        ws.headers = headers
        ws._idle_timeout = idle_timeout
        ws._init_common()
        # A heartbeat passed in ASGI mode is accepted for API symmetry but
        # never drives a timer: the ASGI server owns ping/pong on this path.
        ws._init_heartbeat(None)
        # ASGI mode drives the connection through the send/receive callables, so
        # the raw frame parser's queue is never used.
        ws._receive_queue = None  # type: ignore[assignment]
        ws._asgi_receive = receive
        ws._asgi_send = send
        ws.scope = scope
        ws.path = scope.get("path", "")
        return ws

    @classmethod
    def from_transport(
        cls,
        transport: asyncio.Transport,
        headers: dict[str, str],
        scope: dict[str, Any],
        *,
        path_params: dict[str, Any] | None = None,
        idle_timeout: float | None = None,
        recv_queue_maxsize: int | None = None,
    ) -> WebSocket:
        """Construct a raw-transport WebSocket whose 101 was already sent.

        Used by the native `HttpProtocol` upgrade path. The protocol writes the
        RFC 6455 Sec. 4.2.2 101 response synchronously (to switch the byte
        stream) before building this object, so `_handshake_sent` is set: a later
        `accept()` validates state but does not emit a second handshake. The
        connection is otherwise a normal raw-mode `WebSocket` - `transport` is
        set, `_asgi_send` stays `None` (so `_is_asgi` is False), and inbound
        bytes flow through `feed_data`/`_parse_frame` exactly as for a directly
        constructed instance.

        `headers` are the lowercased handshake headers (latin-1 decoded by the
        protocol). `scope` mirrors the ASGI websocket scope shape so the same
        `path`/`query_params`/`client`/`cookies` accessors work unchanged.
        """
        ws = cls(
            transport, headers, recv_queue_maxsize=recv_queue_maxsize, idle_timeout=idle_timeout
        )
        ws._handshake_sent = True
        ws.scope = scope
        ws.path = scope.get("path", "")
        ws.path_params = path_params if path_params is not None else {}
        return ws

    @property
    def _is_asgi(self) -> bool:
        return self._asgi_send is not None

    @property
    def _needs_close(self) -> bool:
        """Whether this side still owes a close frame to the peer.

        Drives the dispatcher's exit handling. `_closed` alone is the wrong
        signal on the raw-transport path: a peer-initiated close sets `_closed`
        from inside the frame parser before the server has sent its own (reply)
        close frame (RFC 6455 Sec. 5.5.1), so guarding the dispatcher's
        `close()` on `not _closed` would suppress that reply and hang the
        handshake. In raw mode the server still owes a close exactly while it
        has not sent one (`_close_frame_sent` is False). In ASGI mode the
        framework never sends a second close after `_closed` is set (a local
        close or a peer disconnect both set it), so `_closed` is the signal.
        """
        if self._is_asgi:
            return not self._closed
        return not self._close_frame_sent

    @property
    def query_params(self) -> Any:
        """Parsed query string of the WebSocket handshake URL.

        Read it as `ws.query_params["token"]`. Backed by
        `QueryParams` (multi-value, `getlist`-aware). Empty when the
        scope carries no `query_string`.
        """
        if self._query_params is not None:
            return self._query_params
        raw = b""
        if self.scope:
            raw = self.scope.get("query_string", b"") or b""
        qs = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
        self._query_params = QueryParams.from_query_string(qs)
        return self._query_params

    @property
    def url(self) -> str:
        """The WebSocket handshake URL path - ASGI-style shape.

        Returns `path` plus `?query` when a query string is present.
        """
        raw = b""
        if self.scope:
            raw = self.scope.get("query_string", b"") or b""
        qs = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
        return f"{self.path}?{qs}" if qs else self.path

    @property
    def client(self) -> Any:
        """The connecting peer as an `Address(host, port)`.

        Reads `scope["client"]` (the ASGI `(host, port)` pair).
        Returns `None` when the scope carries no client info.
        """
        client = self.scope.get("client") if self.scope else None
        if client:
            return Address(client[0], client[1])
        return None

    @property
    def state(self) -> Any:
        """Per-connection scratch namespace.

        Lazily-created `State` (a dict subclass) supporting both
        `ws.state.user = ...` attribute access and `ws.state["user"]`.
        """
        existing = self._state
        if existing is None:
            existing = self._state = State()
        return existing

    @property
    def cookies(self) -> dict[str, str]:
        """Cookies sent with the WebSocket handshake.

        Parses the handshake `Cookie` header into `{name: value}`.
        Empty when no cookie header was present.
        """
        if self._cookies is not None:
            return self._cookies
        self._cookies = parse_cookie(self.headers.get(HEADER_COOKIE.lower(), ""))
        return self._cookies

    @property
    def application_state(self) -> WebSocketState:
        """Server-side state of the WebSocket.

        - `CONNECTING` before the app sends `accept()`.
        - `CONNECTED` after `accept()` and until `close()` is sent.
        - `DISCONNECTED` after `close()` (locally) or after the peer
          half-closes (observed via `WebSocketDisconnect`).
        """
        if self._closed:
            return WebSocketState.DISCONNECTED
        if self._accepted:
            return WebSocketState.CONNECTED
        return WebSocketState.CONNECTING

    @property
    def client_state(self) -> WebSocketState:
        """Client-side state of the WebSocket.

        Veloce does not distinguish the two halves at the protocol level
        beyond the close flag, so this mirrors `application_state` once
        the peer disconnects and otherwise stays `CONNECTED` once the
        handshake completes.
        """
        return self.application_state

    @property
    def origin(self) -> str | None:
        """The client-supplied `Origin` header, or `None` if absent.

        WebSocket handshakes carry `Origin` per RFC 6455 Sec. 10.2 / Sec. 4.1.
        Browsers always send it; non-browser clients may omit it. The
        header is the application's primary defence against Cross-Site
        WebSocket Hijacking - CSWSH bypasses CORS because the handshake
        is plain HTTP/1.1 and Same-Origin Policy does not apply to it.
        Pair this accessor with `check_origin(allowed)` before `accept()`.
        """
        return self.headers.get(HEADER_ORIGIN.lower())

    def check_origin(self, allowed: str | Iterable[str]) -> bool:
        """Return `True` when the handshake's `Origin` is in `allowed`.

        Pass a single origin string or an iterable of allowed origins
        (e.g. `["https://app.example.com", "https://admin.example.com"]`).
        Normalisation matches `WebSocketOriginMiddleware`: each side is
        lowercased *and* has any trailing slash stripped, so allow-lists
        written for one API are interchangeable with the other.

        - **Wildcard.** `"*"` in `allowed` accepts any origin and is the
          opt-in "I have my own check elsewhere" escape hatch - the
          symmetric behaviour to `WebSocketOriginMiddleware`'s
          `allowed_origins=["*"]`.
        - **Missing `Origin`** (no header at all, or a literal
          `Origin: null` from a sandboxed iframe / `file://` page) is
          a non-match and returns `False`. Non-browser clients
          legitimately omit the header - if you want to allow them,
          branch on `ws.origin is None` explicitly. The
          `WebSocketOriginMiddleware` middleware path also offers an
          `allow_missing=True` switch; this in-handler helper is
          deliberately strict-by-default.

        Usage:
            @app.websocket("/ws")
            async def chat(ws: WebSocket):
                if not ws.check_origin("https://app.example.com"):
                    await ws.close(code=WS_1008_POLICY_VIOLATION)  # policy violation
                    return
                await ws.accept()
                ...

        For the middleware-style check (registered once, runs before
        the handler) reach for `veloce.SecurityHeadersMiddleware`'s
        sibling `WebSocketOriginMiddleware`.
        """
        allowed_set = frozenset((allowed,)) if isinstance(allowed, str) else frozenset(allowed)
        # Wildcard short-circuit - accept anything, including a missing
        # `Origin`. Matches the middleware's `_allow_all` branch.
        if "*" in allowed_set:
            return True
        if self.origin is None or self.origin == "null":
            return False
        origin_norm = self.origin.rstrip("/").lower()
        normalised = {a.rstrip("/").lower() for a in allowed_set}
        return origin_norm in normalised

    @property
    def requested_subprotocols(self) -> list[str]:
        """Subprotocols the client offered in `Sec-WebSocket-Protocol`.

        Returns them in client preference order (RFC 6455 Sec. 1.9). Empty
        list when the header is absent. Whitespace around each token is
        stripped; the comparison the negotiator performs is case-sensitive
        per the spec.
        """
        raw = self.headers.get(HEADER_SEC_WEBSOCKET_PROTOCOL.lower(), "")
        if not raw:
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    def negotiate_subprotocol(self, supported: list[str]) -> str | None:
        """Pick the first client-offered subprotocol that the server supports.

        Per RFC 6455 Sec. 4.1, the server picks ONE protocol from the client's
        list. Most servers prefer to honour the client's preference order
        (first match wins), which is what we do.
        """
        offered = self.requested_subprotocols
        if not offered:
            return None
        wanted = set(supported)
        for proto in offered:
            if proto in wanted:
                return proto
        return None

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Complete the WebSocket handshake.

        Records the chosen subprotocol on `accepted_subprotocol`.

        Raises:
            RuntimeError: if the connection is already accepted or already
                closed, or if a ``subprotocol``/``headers`` argument is passed
                on the native (``Veloce.run``) upgrade path, where the 101
                response has already been sent and cannot be renegotiated.
        """
        # Enforce the handshake state machine: accepting an already-accepted
        # or already-closed connection is a programming error - surface it
        # as a clear exception rather than re-running the handshake.
        if self._accepted:
            raise RuntimeError("WebSocket.accept(): connection is already accepted")
        if self._closed:
            raise RuntimeError("WebSocket.accept(): connection is already closed")
        # Reject CR/LF in the negotiated subprotocol and any custom
        # handshake headers - they are written into the 101 response.
        if subprotocol:
            _reject_header_crlf(subprotocol, "WebSocket subprotocol")
        if headers:
            for _k, _v in headers.items():
                _reject_header_crlf(_k, "WebSocket header name")
                _reject_header_crlf(_v, "WebSocket header value")

        if self._is_asgi:
            # ASGI: consume the connect message, then emit accept.
            msg = await self._asgi_receive()
            if msg["type"] != ASGI_EVENT_WS_CONNECT:
                raise RuntimeError(f"expected {ASGI_EVENT_WS_CONNECT}, got {msg['type']!r}")
            accept_msg: dict[str, Any] = {"type": ASGI_EVENT_WS_ACCEPT}
            if subprotocol:
                accept_msg["subprotocol"] = subprotocol
            if headers:
                accept_msg["headers"] = [
                    (k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()
                ]
            await self._asgi_send(accept_msg)
            self.accepted_subprotocol = subprotocol
            self._accepted = True
            return

        if self._handshake_sent:
            # Native upgrade path: `HttpProtocol` already wrote the 101
            # synchronously to switch the byte stream, so there is no second
            # handshake to emit. The 101 is already on the wire, so a
            # subprotocol or extra header passed here cannot be appended to it -
            # fail loud rather than silently drop the negotiation. Native
            # subprotocol negotiation is a documented limitation; run under an
            # ASGI server (uvicorn/hypercorn) for that case.
            if subprotocol is not None or headers is not None:
                raise RuntimeError(
                    "WebSocket.accept(): native (Veloce.run) WebSocket upgrade does not "
                    "support negotiating a subprotocol or custom handshake headers - the 101 "
                    "response was already sent. Run under an ASGI server (uvicorn/hypercorn) "
                    "for native subprotocol negotiation."
                )
            self._accepted = True
            return

        # Raw-transport mode (HTTP/1.1 101 handshake).
        key = self.headers.get(HEADER_SEC_WEBSOCKET_KEY.lower(), "")
        accept_key = compute_accept(key)

        lines = [
            "HTTP/1.1 101 Switching Protocols",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Accept: {accept_key}",
        ]
        if subprotocol:
            lines.append(f"Sec-WebSocket-Protocol: {subprotocol}")
        if headers:
            for k, v in headers.items():
                lines.append(f"{k}: {v}")
        response = "\r\n".join(lines) + "\r\n\r\n"
        self.transport.write(response.encode())
        self.accepted_subprotocol = subprotocol
        self._accepted = True
        # Arm the liveness probe now the connection is live (no-op unless a
        # heartbeat was configured for this raw-transport connection).
        self.start_heartbeat()

    async def send_text(self, data: str) -> None:
        """Send a text frame."""
        if not self._accepted:
            raise RuntimeError("WebSocket.send_text(): call accept() before sending")
        if self._closed:
            raise WebSocketDisconnect()
        if self._asgi_send is not None:
            # Sent here rather than through `_asgi_send_safe`, for the same reason
            # the receive path is inlined: this is the frame every message pays.
            # The error handling itself stays in `_asgi_send_failed`.
            try:
                await self._asgi_send({"type": ASGI_EVENT_WS_SEND, "text": data})
            except (ConnectionError, OSError) as exc:
                self._asgi_send_failed(exc)
            return
        await self._raw_send(data.encode("utf-8"), opcode=0x1)

    async def send_json(self, data: Any, mode: str = "text") -> None:
        """Send JSON data.

        `mode="text"` (default) wraps the JSON in a text frame (opcode 0x1).
        `mode="binary"` sends the raw JSON bytes as a binary frame (0x2).
        """
        if mode not in ("text", "binary"):
            raise ValueError(f"mode must be 'text' or 'binary', got {mode!r}")
        # The shared encoder, so a frame carries the same JSON dialect the
        # application's responses do. `app` is set for a dispatched connection
        # and `None` for a socket built outside one.
        payload = dumps_for(self.app, data)
        if mode == "binary":
            await self.send_bytes(payload)
        else:
            await self.send_text(payload.decode("utf-8"))

    async def send_bytes(self, data: bytes) -> None:
        """Send a binary frame."""
        if not self._accepted:
            raise RuntimeError("WebSocket.send_bytes(): call accept() before sending")
        if self._closed:
            raise WebSocketDisconnect()
        if self._asgi_send is not None:
            # Sent here rather than through `_asgi_send_safe`, for the same reason
            # the receive path is inlined: this is the frame every message pays.
            # The error handling itself stays in `_asgi_send_failed`.
            try:
                await self._asgi_send({"type": ASGI_EVENT_WS_SEND, "bytes": data})
            except (ConnectionError, OSError) as exc:
                self._asgi_send_failed(exc)
            return
        await self._raw_send(data, opcode=0x2)

    async def _asgi_send_safe(self, message: dict) -> None:
        """Forward an ASGI send, normalizing a dead-peer OSError to a disconnect.

        Under an ASGI server a send to a peer that has gone away surfaces as an
        ``OSError`` / ``ConnectionError`` (broken pipe, connection reset) from
        the transport. Normalize it to ``WebSocketDisconnect`` so handlers catch
        the same exception on every transport instead of a raw socket error, and
        mark the socket closed so subsequent sends short-circuit.
        """
        try:
            await self._asgi_send(message)
        except (ConnectionError, OSError) as exc:
            self._asgi_send_failed(exc)

    def _asgi_send_failed(self, exc: BaseException) -> NoReturn:
        """Turn a dead-peer transport error into a disconnect and mark us closed.

        The one place that normalization happens, shared with the inline send
        path below so the two cannot come to disagree about what a broken pipe
        means to a handler.
        """
        self._closed = True
        raise WebSocketDisconnect(WS_1006_ABNORMAL_CLOSURE) from exc

    def _asgi_disconnected(self, msg: dict) -> NoReturn:
        """Record the peer's close from a `websocket.disconnect` and raise.

        The one place ASGI peer-close is turned into connection state, so the
        unbounded receive path below - which reads the message itself to stay
        one coroutine frame deep - shares this rather than repeating it.
        """
        self._closed = True
        # The ASGI server reports the peer's close code (and, on newer
        # servers, the reason). Expose them via the same accessors the
        # raw-transport path populates so handlers see one API.
        code = msg.get("code", WS_1005_NO_STATUS_RCVD)
        self.close_code = code
        self.close_reason = msg.get("reason", "") or ""
        raise WebSocketDisconnect(code)

    async def _asgi_recv_msg(self) -> dict:
        msg = await self._asgi_receive()
        if msg["type"] == ASGI_EVENT_WS_DISCONNECT:
            self._asgi_disconnected(msg)
        return msg

    async def receive(self) -> dict[str, Any]:
        """Receive a raw ASGI WebSocket message.

        Returns the message dict as the ASGI server delivered it
        (`{"type": "websocket.receive", "text"/"bytes": ...}`). A
        `websocket.disconnect` message raises `WebSocketDisconnect`.
        ASGI-mode only - raw asyncio-transport connections don't carry
        ASGI message envelopes.

        The same handshake state machine the typed `receive_*` helpers
        enforce: the raw escape hatch must not be a way around
        receive-before-accept or receive-after-close (which would
        consume the `websocket.connect` envelope and corrupt the next
        `accept()`).
        """
        self._check_can_receive("receive")
        if not self._is_asgi:
            raise RuntimeError(
                "WebSocket.receive() is ASGI-mode only; use receive_text/"
                "receive_bytes for raw asyncio-transport connections"
            )
        if self._idle_timeout is None:
            return await self._asgi_recv_msg()
        try:
            return await asyncio.wait_for(self._asgi_recv_msg(), timeout=self._idle_timeout)
        except (TimeoutError, asyncio.TimeoutError):
            # `_idle_close` always raises WebSocketDisconnect after the
            # 1001 close handshake, so control never returns here.
            await self._idle_close()

    async def send(self, message: dict[str, Any]) -> None:
        """Send a raw ASGI WebSocket message.

        `message` is forwarded straight to the ASGI `send` callable,
        e.g. `{"type": "websocket.send", "text": "..."}`.
        """
        # Same handshake state machine the typed send_* helpers enforce:
        # the raw escape hatch must not be a way around accept-before-send
        # or send-after-close.
        if not self._accepted:
            raise RuntimeError("WebSocket.send(): call accept() before sending")
        if self._closed:
            raise WebSocketDisconnect()
        if not self._is_asgi:
            raise RuntimeError(
                "WebSocket.send() is ASGI-mode only; use send_text/send_bytes "
                "for raw asyncio-transport connections"
            )
        await self._asgi_send_safe(message)

    def _check_can_receive(self, method: str) -> None:
        """Enforce the handshake state machine for receive operations.

        Mirrors the `send_text`/`send_bytes` guards - a handler that
        calls a receive method before `accept()` would otherwise hang on
        an empty queue (raw transport) or on the ASGI receive callable
        (ASGI mode), with no clear failure mode. A receive on a
        already-closed connection is a `WebSocketDisconnect`.
        """
        if not self._accepted:
            raise RuntimeError(f"WebSocket.{method}(): call accept() before receiving")
        if self._closed:
            # Surface the recorded peer/close code (1001, 1006, ...) when a
            # close arrived between receives, not a default 1000 - matching the
            # `_raw_recv` disconnect path.
            raise WebSocketDisconnect(self.close_code or WS_1000_NORMAL_CLOSURE)

    def set_send_drain(self, drain: Any) -> None:
        """Install the native write-side backpressure hook (raw transport only).

        `drain` is an awaitable-returning callable (`HttpProtocol.drain`) that
        blocks while the transport's outgoing buffer is over its high-water
        mark. The async `send_*` wrappers await it before writing each frame, so
        a slow-reading client suspends the producing handler instead of letting
        the transport buffer grow without bound. The native upgrade path
        (`HttpProtocol`) calls this once; ASGI mode leaves it unset.
        """
        self._send_drain = drain

    def set_idle_timeout(self, idle_timeout: float | None) -> None:
        """Set the idle-receive timeout in seconds (`None` disables it).

        Applies to every subsequent blocking receive on this connection.
        Call it inside the handler (typically right after `accept()`) to
        enable or adjust the window; passing `idle_timeout=` to
        `WebSocket.from_asgi` sets the same value at construction.
        """
        _validate_idle_timeout(idle_timeout)
        self._idle_timeout = idle_timeout

    def _effective_timeout(self, timeout: float | None) -> float | None:
        """Bound a per-call timeout by the connection's idle timeout.

        Returns the smaller of the explicit per-call `timeout` and the
        configured `idle_timeout`; either being `None` falls back to the
        other. The result drives `asyncio.wait_for` so a silent peer trips
        whichever deadline is shorter.
        """
        idle = self._idle_timeout
        if idle is None:
            return timeout
        if timeout is None:
            return idle
        return min(timeout, idle)

    async def _idle_close(self) -> NoReturn:
        """Close cleanly on an idle timeout and signal the handler.

        Performs the RFC 6455 close handshake with `1001 Going Away`
        (never a fabricated 1006) so the peer sees a graceful shutdown,
        then raises `WebSocketDisconnect` so the receive call and any
        `iter_*` loop unwind exactly as on a peer-initiated close.
        """
        with contextlib.suppress(Exception):
            await self.close(code=WS_1001_GOING_AWAY)
        raise WebSocketDisconnect(WS_1001_GOING_AWAY)

    async def receive_text(self, timeout: float | None = None) -> str:
        """Receive a text message. Raises asyncio.TimeoutError if timeout exceeded.

        When `idle_timeout` is configured, a wait longer than the idle
        window closes the connection with `1001 Going Away` and raises
        `WebSocketDisconnect` instead of `asyncio.TimeoutError`.
        """
        self._check_can_receive("receive_text")
        if self._asgi_send is not None:
            # The ASGI receive is taken here rather than through the timeout and
            # envelope wrappers: with no deadline to arm there is nothing for them
            # to do, and this is the frame every message on every connection pays.
            # The peer-close handling itself stays in `_asgi_disconnected`.
            if timeout is None and self._idle_timeout is None:
                msg = await self._asgi_receive()
                if msg["type"] == ASGI_EVENT_WS_DISCONNECT:
                    self._asgi_disconnected(msg)
            else:
                msg = await self._asgi_recv_text_or_bytes(timeout)
            return msg.get("text") or (msg.get("bytes") or b"").decode("utf-8")
        data = await self._raw_recv(timeout)
        return data.decode("utf-8") if isinstance(data, bytes) else str(data)

    async def _raw_recv(self, timeout: float | None) -> bytes:
        """Receive one complete message in raw mode, bounded by the idle window.

        The wait is bounded by the smaller of the per-call `timeout` and the
        connection's `idle_timeout`. In raw-transport mode the idle window is
        measured per completed message, so a peer that only streams long
        fragmented messages or keep-alive control frames can still trip it -
        this is acceptable because raw transport is not the deployed path
        (production WebSockets run over ASGI, where the server delivers
        complete messages and owns ping/pong). An idle expiry performs the
        `1001 Going Away` close; a binding per-call `timeout` raises
        `asyncio.TimeoutError` unchanged.
        """
        eff = self._effective_timeout(timeout)
        if eff is None:
            item = await self._receive_queue.get()
        else:
            try:
                item = await asyncio.wait_for(self._receive_queue.get(), timeout=eff)
            except (TimeoutError, asyncio.TimeoutError):
                await self._maybe_idle_timeout(timeout, eff)
                raise
        # The frame parser enqueues `_RAW_DISCONNECT` when the connection
        # closes (peer close frame, oversized/protocol-error/invalid-payload
        # close, or a heartbeat timeout on a dead peer) to wake a parked
        # receiver; surface it as the disconnect the handler expects, carrying
        # the recorded close code so the handler unwinds like a peer close.
        # A close frame may omit the status code, so fall back to a normal 1000.
        if item is _RAW_DISCONNECT:
            raise WebSocketDisconnect(self.close_code or WS_1000_NORMAL_CLOSURE)
        return cast("bytes", item)

    async def _maybe_idle_timeout(self, timeout: float | None, eff: float | None) -> None:
        """Treat a `wait_for` timeout as an idle close when idle won the race.

        A timeout only means "idle" when the effective deadline came from
        the idle window rather than a smaller explicit per-call `timeout`.
        When the per-call `timeout` was the binding (smaller) deadline the
        original `asyncio.TimeoutError` propagates to the caller unchanged.
        """
        if self._idle_timeout is not None and (timeout is None or eff == self._idle_timeout):
            await self._idle_close()

    async def _asgi_recv_text_or_bytes(self, timeout: float | None) -> dict:
        """Receive one ASGI message, bounding the wait only when a deadline is set.

        The common case has neither a per-call `timeout` nor a configured
        `idle_timeout`, so the receive awaits directly and skips the
        `asyncio.wait_for` Task wrapper that only the bounded case needs.
        """
        eff = self._effective_timeout(timeout)
        if eff is None:
            return await self._asgi_recv_msg()
        try:
            return await asyncio.wait_for(self._asgi_recv_msg(), timeout=eff)
        except (TimeoutError, asyncio.TimeoutError):
            await self._maybe_idle_timeout(timeout, eff)
            raise

    async def receive_json(self, timeout: float | None = None) -> Any:
        """Receive and parse JSON."""
        # Routes through `receive_text`, which enforces the state guards.
        text = await self.receive_text(timeout=timeout)
        return orjson.loads(text)

    async def receive_bytes(self, timeout: float | None = None) -> bytes:
        """Receive binary data. Raises asyncio.TimeoutError if timeout exceeded.

        When `idle_timeout` is configured, a wait longer than the idle
        window closes the connection with `1001 Going Away` and raises
        `WebSocketDisconnect` instead of `asyncio.TimeoutError`.
        """
        self._check_can_receive("receive_bytes")
        if self._asgi_send is not None:
            # The ASGI receive is taken here rather than through the timeout and
            # envelope wrappers: with no deadline to arm there is nothing for them
            # to do, and this is the frame every message on every connection pays.
            # The peer-close handling itself stays in `_asgi_disconnected`.
            if timeout is None and self._idle_timeout is None:
                msg = await self._asgi_receive()
                if msg["type"] == ASGI_EVENT_WS_DISCONNECT:
                    self._asgi_disconnected(msg)
            else:
                msg = await self._asgi_recv_text_or_bytes(timeout)
            return msg.get("bytes") or msg.get("text", "").encode("utf-8")
        return await self._raw_recv(timeout)

    async def iter_text(self) -> Any:
        """Async-iterate over incoming text frames until the peer closes.

        Usage:
            async for msg in ws.iter_text():
                ...

        Terminates cleanly on `WebSocketDisconnect`. Other exceptions
        propagate.
        """
        try:
            while True:
                yield await self.receive_text()
        except WebSocketDisconnect:
            return

    async def iter_bytes(self) -> Any:
        """Async-iterate over incoming binary frames until the peer closes."""
        try:
            while True:
                yield await self.receive_bytes()
        except WebSocketDisconnect:
            return

    async def iter_json(self) -> Any:
        """Async-iterate over incoming JSON-decoded frames until peer closes."""
        try:
            while True:
                yield await self.receive_json()
        except WebSocketDisconnect:
            return

    async def close(self, code: int = WS_1000_NORMAL_CLOSURE, reason: str = "") -> None:
        """Send a close frame and complete the RFC 6455 close handshake.

        Per RFC 6455 Sec. 5.5.1 the close-frame payload is a 2-byte big-endian
        status code optionally followed by a UTF-8 reason of at most
        123 bytes (so the whole payload fits in the 125-byte
        control-frame budget). Reasons longer than 123 bytes are
        truncated to a clean UTF-8 boundary.

        On the raw-transport path the close is a full handshake (Sec. 5.5.1,
        Sec. 7.1.1): the close frame is sent, then a server-initiated close
        waits for the peer's reply close frame (bounded by
        `CLOSE_HANDSHAKE_TIMEOUT`) before dropping the TCP connection. A
        peer-initiated close already carries the peer's frame, so the reply is
        sent and the transport closed without waiting.
        """
        code, reason = _sanitise_close(code, reason)
        if self._is_asgi:
            if self._closed:
                return
            self._closed = True
            self._cancel_heartbeat()
            await self._asgi_send(
                {"type": ASGI_EVENT_WS_CLOSE, "code": code, "reason": reason or ""}
            )
            return
        # Raw transport. `_closed` may already be set (peer-initiated close set
        # it from the frame parser), so the reply frame is keyed off
        # `_close_frame_sent`, not `_closed`, to avoid returning early before the
        # server has answered the peer's close.
        if self._close_frame_sent:
            return
        peer_started = self._peer_closed
        self._closed = True
        self._close_frame_sent = True
        self._cancel_heartbeat()
        # Arm the reply-wait before sending our frame so a peer close frame that
        # arrives during/right after the send is not missed (the parser sets the
        # event only when it is already present). Skipped when the peer started
        # the close - its frame is already in hand.
        if not peer_started:
            self._peer_close_event = asyncio.Event()
        payload = struct.pack("!H", code)
        if reason:
            # Already clamped to the control-frame budget on a codepoint
            # boundary by `_sanitise_close`, above the transport branch.
            payload += reason.encode("utf-8")
        with contextlib.suppress(Exception):
            self._send_frame(payload, opcode=0x8)
        # Server-initiated close: await the peer's reply close frame so both
        # sides agree the connection is closing before the TCP socket drops
        # (RFC 6455 Sec. 7.1.1). The frame parser sets `_peer_close_event` when
        # the peer's close arrives; a silent peer trips the bounded timeout.
        if not peer_started and self._peer_close_event is not None:
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._peer_close_event.wait(), timeout=self.CLOSE_HANDSHAKE_TIMEOUT
                )
        if self.transport is not None:
            self.transport.close()

    def feed_data(self, data: bytes) -> None:
        """Feed raw bytes from the transport (called by the protocol).

        The transport delivers byte runs that need not align with frame
        boundaries: a single frame may be split across two reads, and one
        read may carry several frames. Bytes are appended to a persistent
        receive buffer and complete frames are parsed off the front in a
        loop - partial frames are kept for the next call.

        Handles fragmented messages (RFC 6455 Sec. 5.4): a data frame with
        `FIN=0` opens a message that subsequent continuation frames
        (opcode `0x0`) extend, and the `FIN=1` continuation completes it.
        Control frames (close / ping / pong) are never fragmented and may
        be interleaved within a fragmented message without disturbing the
        reassembly buffer.
        """
        if not data:
            return
        # Any inbound byte proves the socket is alive; defer the next
        # heartbeat probe (no-op when heartbeat is disabled).
        self._note_heartbeat_inbound()
        self._recv_buffer += data
        # Parse as many whole frames as the buffer now holds. `_parse_frame`
        # returns the number of bytes it consumed (0 when the buffer does
        # not yet hold a complete frame) and may set `_closed` on a close /
        # backpressure event, after which we stop. A single read can carry
        # many small frames, so advance a local offset per frame and compact
        # the buffer once at the end - slicing after every frame would memmove
        # the remaining tail repeatedly, making a multi-frame read O(k * n).
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

        Returns the number of buffer bytes (from `start`) the frame
        occupied, or `0` when the buffer does not yet hold a complete frame
        (the caller keeps the bytes for the next `feed_data`). Parsing reads
        relative to `start` so `feed_data` can walk several frames in one
        read without re-slicing the buffer per frame. A complete frame is
        unmasked and dispatched per the close / ping / pong / data
        semantics before returning.
        """
        buf = self._recv_buffer
        n = len(buf) - start
        if n < 2:
            return 0

        fin = bool(buf[start] & 0x80)
        opcode = buf[start] & 0x0F
        masked = bool(buf[start + 1] & 0x80)
        payload_len = buf[start + 1] & 0x7F

        # RFC 6455 Sec. 5.2: RSV1-3 (mask 0x70) MUST be zero unless an
        # extension that defines them was negotiated. Veloce negotiates no
        # permessage-deflate / extension, so any reserved bit set is a 1002
        # protocol error. Rejected before length resolution / allocation.
        if buf[start] & 0x70:
            self._close_protocol_error()
            return 0

        # RFC 6455 Sec. 5.1: every client-to-server frame MUST be masked, and a
        # server that receives an unmasked frame fails the connection with a
        # 1002 protocol error. `feed_data` is driven only by the server-side
        # transport, so an unmasked frame is a non-conformant or hostile client;
        # reject it here - before length resolution / payload allocation -
        # rather than processing it as a valid message.
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

        # Bound the declared length before waiting for / allocating the
        # payload - a huge declared length must not park unbounded bytes in
        # the buffer or blow up the reassembly buffer.
        if payload_len > self.MAX_FRAME_SIZE:
            self._close_too_big()
            return 0

        # Control frames (close / ping / pong) must carry <=125 bytes and
        # must not be fragmented (RFC 6455 Sec. 5.5). The new reliable parser
        # hits these consistently, so reject violations with a 1002 close
        # rather than, e.g., echoing an oversized ping as a pong.
        if opcode in (0x8, 0x9, 0xA) and (payload_len > 125 or not fin):
            self._close_protocol_error()
            return 0

        if masked:
            if n < offset + 4:
                return 0
            mask = buf[start + offset : start + offset + 4]
            offset += 4

        frame_len = offset + payload_len
        if n < frame_len:
            return 0

        payload = bytes(buf[start + offset : start + offset + payload_len])
        if masked and payload_len:
            payload = _unmask(payload, mask, payload_len)

        # Control frames (close / ping / pong) - never fragmented; handled
        # independently of any fragmented message in progress.
        if opcode == 0x8:  # Close
            # Peer-initiated close (RFC 6455 Sec. 5.5.1). Record that the peer
            # started the handshake and unblock any server-initiated `close()`
            # already awaiting the peer's reply, then validate the close payload,
            # echo the reply close frame, record `close_code`/`close_reason`, and
            # wake any parked receiver so the handler unwinds via
            # `WebSocketDisconnect`. `_handle_close_frame` sends the reply and
            # sets `_close_frame_sent`, so `_run_websocket`'s clean-exit
            # `close()` is a no-op rather than emitting a second close frame.
            self._peer_closed = True
            if self._peer_close_event is not None:
                self._peer_close_event.set()
            self._handle_close_frame(payload)
            raise WebSocketDisconnect(self.close_code or WS_1000_NORMAL_CLOSURE)
        if opcode == 0x9:  # Ping
            self._send_frame(payload, opcode=0xA)  # Pong
            return frame_len
        if opcode == 0xA:  # Pong
            # A PONG echoing the outstanding heartbeat token confirms the
            # peer answered this window's probe; clear the token so the next
            # idle window issues a fresh PING instead of faulting the peer.
            # (Any inbound frame already defers the probe via `feed_data`;
            # the token match is the precise confirmation.)
            if self._hb_token is not None and payload == self._hb_token.to_bytes(4, "big"):
                self._hb_token = None
            return frame_len

        # Data frames (text / binary) and continuation frames.
        if opcode in (0x1, 0x2):
            # A data frame must not arrive mid-fragmentation - RFC 6455
            # Sec. 5.4 allows only continuation frames (opcode 0x0) after the
            # opening frame of a fragmented message. A new data frame while a
            # fragmented message is in progress is a protocol error and fails
            # the connection with 1002, the symmetric case to a continuation
            # frame with no message in progress.
            if self._frag_opcode is not None:
                self._close_protocol_error()
                return 0
            if opcode == 0x1:
                # TEXT payloads must be valid UTF-8 (RFC 6455 Sec. 8.1).
                # Validate this opening/whole frame's bytes incrementally so
                # a bad byte trips here, not at receive_text() decode time.
                validator = _Utf8Validator()
                if not validator.feed(payload):
                    self._close_invalid_payload()
                    return 0
            else:
                validator = None
            if fin:
                # Unfragmented message - deliver immediately. A TEXT frame
                # must end on a codepoint boundary.
                if validator is not None and not validator.done:
                    self._close_invalid_payload()
                    return 0
                self._frag_opcode = None
                self._frag_buffer = bytearray()
                self._frag_validator = None
                self._enqueue_or_close(payload)
            else:
                # Opening frame of a fragmented message - start buffering
                # (supersedes any abandoned partial).
                self._frag_opcode = opcode
                self._frag_buffer = bytearray(payload)
                self._frag_validator = validator
                if len(self._frag_buffer) > self.MAX_MESSAGE_SIZE:
                    self._close_too_big()
                    return 0
        elif opcode == 0x0:  # Continuation frame.
            if self._frag_opcode is None:
                # RFC 6455 Sec. 5.4: a continuation frame with no message in
                # progress is a protocol error - close with 1002.
                self._close_protocol_error()
                return 0
            # Validate continuation bytes against the in-progress message's
            # UTF-8 state (a no-op for a binary message, whose validator is
            # None) before appending.
            if self._frag_validator is not None and not self._frag_validator.feed(payload):
                self._close_invalid_payload()
                return 0
            self._frag_buffer += payload
            # Cap the cumulative reassembled size: the per-frame cap bounds
            # one frame, but a stream of continuation frames could otherwise
            # grow the buffer without limit (unbounded-allocation DoS).
            if len(self._frag_buffer) > self.MAX_MESSAGE_SIZE:
                self._close_too_big()
                return 0
            if fin:
                # Final fragment - the reassembled message is complete. A
                # TEXT message must end on a codepoint boundary.
                if self._frag_validator is not None and not self._frag_validator.done:
                    self._close_invalid_payload()
                    return 0
                self._enqueue_or_close(bytes(self._frag_buffer))
                self._frag_opcode = None
                self._frag_buffer = bytearray()
                self._frag_validator = None

        # The frame was fully consumed regardless of opcode-specific
        # handling - report its length so the caller drops it and looks
        # for the next frame in the buffer.
        return frame_len

    def _terminate_raw(self, code: int, *, record_close_code: bool) -> None:
        """Synchronously send a close frame carrying `code`, then drop the transport.

        The single raw-mode teardown core. No `await` is available from inside
        the Protocol callback that drives `feed_data`, so the close is
        synchronous and mirrors `_enqueue_or_close`: emit the close frame, cancel
        the heartbeat, mark the connection closed, wake any parked receiver, and
        close the transport. `record_close_code` writes `code` into `close_code`
        for the locally-initiated close paths (`_close_control`); the peer-reply
        path (`_echo_close`) leaves `close_code` to `_handle_close_frame`, which
        records the peer's own code.
        """
        with contextlib.suppress(Exception):
            self._send_frame(code.to_bytes(2, "big"), opcode=0x8)  # Close
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
        big-endian status code optionally followed by a UTF-8 reason. An
        empty payload means "no status" (recorded as 1005, the reserved
        "no status received" code, without putting it on the wire). A
        1-byte payload, a status code the peer is not allowed to send, or a
        non-UTF-8 reason is a protocol error answered with a 1002 close.
        Otherwise the connection is closed by echoing a normal 1000 close
        and `close_code`/`close_reason` are exposed to the handler.
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
        # RFC 6455 Sec. 7.4.2: a peer may send a registered code (handled by
        # the allow-list) or a private code in 3000-4999; anything below 1000,
        # an unassigned/reserved code below 3000, or a code above 4999 is a
        # protocol violation answered with 1002.
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
        # RFC 6455 Sec. 5.5.1: the reply Close SHOULD echo the peer's status
        # code (a private/registered code the peer is allowed to send).
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

        The queue has a finite `maxsize`; if a peer outpaces the
        application reader it fills up. `put_nowait` raising
        `QueueFull` is the backpressure signal at this layer - we
        close the connection with `1009 Message Too Big` rather than
        let the exception unwind into the asyncio Protocol callback,
        and rather than grow the queue without bound (the DoS the
        cap was added to prevent).
        """
        try:
            self._receive_queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Close synchronously - no `await` available from inside
            # `feed_data`. The frame writer is synchronous and only
            # needs the transport.
            self._close_too_big()

    # ── Heartbeat (raw-transport liveness) ──
    #
    # An opt-in proactive liveness probe for the raw-transport path. A
    # black-holed TCP connection (peer vanished without FIN/RST - common
    # behind NAT/load balancers) otherwise parks a handler in `receive_*`
    # forever. When `heartbeat` is set, a timer periodically sends an
    # application PING carrying a monotonically increasing token; the peer's
    # PONG must echo that token within the next window. ANY inbound byte
    # (proof the socket is alive) defers the next probe, so a busy connection
    # never pays for needless pings. Two consecutive windows with no inbound
    # traffic and no matching PONG mark the peer dead and tear the connection
    # down. ASGI deployments leave ping/pong to the server, so the timer is
    # inert there.

    def _init_heartbeat(self, heartbeat: float | None) -> None:
        """Initialise heartbeat state; called from every constructor path."""
        self._heartbeat = heartbeat
        # The `call_later` handle for the next probe tick, or `None` when no
        # heartbeat is armed.
        self._hb_handle = None
        # Token written into the most recent outstanding PING body; `None`
        # when no PING is awaiting a PONG. A PONG echoing this exact token
        # (or any other inbound frame) proves liveness.
        self._hb_token = None
        # Monotonic source for the next PING token.
        self._hb_next_token = 0
        # Set by `feed_data` when inbound bytes arrive between ticks; the next
        # tick treats this as proof of life and skips the dead-peer teardown.
        self._hb_saw_inbound = False

    def start_heartbeat(self) -> None:
        """Arm the heartbeat timer for a raw-transport connection.

        Idempotent and a no-op in ASGI mode or when `heartbeat` was not
        configured. `accept()` calls this automatically once the raw
        handshake completes, so handlers rarely call it directly; it is
        public so a handler that builds a `WebSocket` by hand can start the
        probe after wiring its own transport.
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
        # A running loop is always present: both call sites (`start_heartbeat`
        # off `accept()`, and the `call_later` tick) run inside the event loop.
        loop = asyncio.get_running_loop()
        self._hb_handle = loop.call_later(interval, self._heartbeat_tick)

    def _heartbeat_tick(self) -> None:
        """Run one heartbeat window: detect a dead peer or send a probe.

        Three outcomes per tick:
        - inbound traffic was seen since the last tick -> the peer is alive;
          clear any outstanding PING and re-arm.
        - a PING is still outstanding from the previous window with no
          inbound traffic -> the peer is silent; tear down with 1006.
        - the connection is idle and live -> send a fresh tokened PING and
          re-arm to check for its PONG next window.
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
            # A PING from the previous window went unanswered and nothing
            # else arrived: treat the peer as gone.
            self._close_heartbeat_timeout()
            return
        token = self._hb_next_token
        self._hb_next_token = (token + 1) & 0xFFFFFFFF
        self._hb_token = token
        with contextlib.suppress(Exception):
            self._send_frame(token.to_bytes(4, "big"), opcode=0x9)  # Ping
        self._schedule_heartbeat()

    def _note_heartbeat_inbound(self) -> None:
        """Record that inbound bytes arrived (called from `feed_data`).

        Any inbound frame proves the socket is alive, so the next tick should
        not fault the peer. Coalesced to a single flag - no per-byte timer
        churn - and consulted lazily when the timer next fires.
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

        Any synchronous parser-side close (protocol error, invalid UTF-8,
        too-big, peer Close echo) or a heartbeat timeout sets `_closed` and
        drops the transport, but a coroutine already blocked on the receive
        queue would otherwise hang until its own timeout. Deliver the terminal
        sentinel so it unwinds with a `WebSocketDisconnect` carrying
        `close_code`. Raw-transport mode only (ASGI has no receive queue); a
        silent peer leaves the queue empty so the parked getter is woken.
        """
        if self._receive_queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._receive_queue.put_nowait(_RAW_DISCONNECT)

    async def _raw_send(self, data: bytes, opcode: int) -> None:
        """Write one frame on the raw transport, honouring write backpressure.

        Awaits the protocol's write-side drain (installed via `set_send_drain`)
        BEFORE writing, so a slow-reading client - which trips the transport's
        `pause_writing` - suspends the producing handler here instead of letting
        `_send_frame` buffer unbounded bytes in memory. The drain is a no-op when
        the connection is writable, so the common case adds one `is not None`
        check plus an already-set `Event.wait()`. `_send_frame` itself stays
        synchronous: it is also invoked from the sync Protocol callback that
        drives `feed_data` (pong / close replies), where no `await` is available.
        """
        if self._send_drain is not None:
            await self._send_drain()
        self._send_frame(data, opcode=opcode)

    def _send_frame(self, data: bytes, opcode: int) -> None:
        """Send a WebSocket frame.

        The header is built into a small bytearray and the payload is
        handed to the transport via `writelines` - that avoids a
        bytearray.extend copy of the (potentially KiB-sized) payload
        followed by a `bytes(frame)` copy on the way out the door.
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

        # `transport.writelines` (where supported) keeps the header and
        # payload as separate buffers; otherwise fall back to a single
        # concatenated `write` for transports / test fakes that only
        # implement the basic `WriteTransport` API.
        writelines = getattr(self.transport, "writelines", None)
        if writelines is not None:
            writelines((bytes(header), data))
        else:
            self.transport.write(bytes(header) + bytes(data))

    async def __aenter__(self) -> WebSocket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        # On a clean exit close normally (1000). When the block exits via an
        # exception, leave closing to the dispatcher's error handling so the
        # mapped close code (e.g. 1008 policy violation, 1011 internal error)
        # is sent instead of a normal-closure 1000 - closing here first would
        # set `_closed` and make the dispatcher skip its `close()`.
        if exc[0] is None:
            await self.close()


# ── Declarative listener ──
#
# `Router.websocket_listener` wraps a per-message callback into a full
# WebSocket handler: accept, receive-loop, dispatch, clean disconnect. The
# loop builder lives here (next to `WebSocket`/`WebSocketDisconnect`) so the
# router stays free of WebSocket frame internals.

_WS_MODES = frozenset({"text", "bytes", "json"})


def _resolve_listener_callable(
    callback: Any,
) -> tuple[Callable[..., Awaitable[Any]], bool]:
    """Return an async-callable form of `callback` and whether it wants the socket.

    A sync callback is offloaded to the default executor so a blocking
    per-message body never stalls the event loop, matching how the framework
    runs sync HTTP handlers. The socket is passed positionally as the first
    argument when the callback declares a leading `ws`/`socket` parameter or
    accepts two or more positional parameters.
    """
    wants_socket = _callback_wants_socket(callback)
    if _is_async_callable(callback):
        return callback, wants_socket

    async def _async_call(*args: Any) -> Any:
        # `offload` preserves the request-scoped ContextVars a sync HTTP
        # handler sees (`current_app` / `g` / `request`).
        return await offload(callback, *args)

    return _async_call, wants_socket


def _callback_wants_socket(callback: Any) -> bool:
    """Decide whether a listener callback expects the socket as its first arg.

    True when the first positional parameter is named `ws` or `socket`, or
    when the callback accepts two or more positional parameters (so the data
    is the second). A single-parameter `on_receive(data)` callback gets only
    the message.
    """
    # `inspect.signature` already unwraps a callable instance's `__call__`
    # and drops the bound `self`, so it works on plain functions, bound
    # methods, and `__call__`-able objects alike.
    try:
        params = [
            p
            for p in inspect.signature(callback).parameters.values()
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        return False
    if not params:
        return False
    if params[0].name in ("ws", "socket"):
        return True
    return len(params) >= 2


async def _listener_receive(ws: WebSocket, mode: str) -> Any:
    if mode == "text":
        return await ws.receive_text()
    if mode == "bytes":
        return await ws.receive_bytes()
    return await ws.receive_json()


async def _listener_send(ws: WebSocket, mode: str, data: Any) -> None:
    if mode == "text":
        await ws.send_text(data if isinstance(data, str) else str(data))
    elif mode == "bytes":
        await ws.send_bytes(data)
    else:
        await ws.send_json(data)


def build_listener_handler(
    callback: Any,
    *,
    receive: str = "json",
    send: str = "json",
    on_connect: Any = None,
    on_disconnect: Any = None,
) -> Callable[[WebSocket], Coroutine[Any, Any, None]]:
    """Build a WebSocket handler that runs the canonical accept/receive/close loop.

    The returned handler accepts the connection, fires `on_connect`, then
    loops: receive one message in `receive` mode, pass it to `callback`, and
    send the return value in `send` mode when it is not `None`. The loop ends
    on `WebSocketDisconnect`; `on_disconnect` always runs afterwards. A
    callback that returns `None` sends nothing, so a pure consumer needs no
    special casing.
    """
    if receive not in _WS_MODES:
        raise ValueError(f"receive mode must be one of {sorted(_WS_MODES)}, got {receive!r}")
    if send not in _WS_MODES:
        raise ValueError(f"send mode must be one of {sorted(_WS_MODES)}, got {send!r}")

    fn, wants_socket = _resolve_listener_callable(callback)
    connect_fn = _resolve_listener_callable(on_connect)[0] if on_connect is not None else None
    disconnect_fn = (
        _resolve_listener_callable(on_disconnect)[0] if on_disconnect is not None else None
    )

    # Deliberately NOT `functools.wraps(callback)`: the registered handler
    # must present its own `(ws: WebSocket)` signature so the dependency
    # resolver injects the socket. `wraps` sets `__wrapped__`, which
    # `inspect.signature` follows back to the callback's `(data)` shape and
    # makes the resolver try to bind a nonexistent `data` dependency.
    async def listener(ws: WebSocket) -> None:
        await ws.accept()
        try:
            if connect_fn is not None:
                await connect_fn(ws)
            while True:
                data = await _listener_receive(ws, receive)
                result = await (fn(ws, data) if wants_socket else fn(data))
                # A `None` return means "consume only" - never emit a frame
                # for it (sending `null`/empty would be a spurious message).
                if result is not None:
                    await _listener_send(ws, send, result)
        except WebSocketDisconnect:
            # Peer (or idle/heartbeat close) ended the connection - the
            # canonical, non-error way a listener loop terminates.
            pass
        finally:
            if disconnect_fn is not None:
                # Run teardown even if the peer is already gone; a send from
                # inside `on_disconnect` may itself raise, which is fine.
                await disconnect_fn(ws)

    # Borrow the callback's name for routing/OpenAPI introspection without
    # importing its signature (see the no-`wraps` note above).
    listener.__name__ = getattr(callback, "__name__", "listener")
    return listener
