"""WebSocket support — one `WebSocket` over two transports.

The same object serves an ASGI `websocket` scope, where the server owns the
framing and this sends `websocket.send` messages, and the built-in server's raw
transport, where this does the RFC 6455 work itself: the handshake accept key,
frame parsing and masking, fragmentation reassembly, incremental UTF-8
validation of text payloads (RFC 6455 Sec. 5.6), the close handshake, and an
opt-in heartbeat. `_is_asgi` is the branch between them.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import enum
import hashlib
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
from veloce._internal import _reject_header_crlf, dumps_for
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
    from collections.abc import Iterable


# RFC 6455 Sec. 1.3: `Sec-WebSocket-Accept` is the base64 SHA-1 of the client's
# `Sec-WebSocket-Key` concatenated with this GUID.
_WS_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def compute_accept(key: str) -> str:
    """Compute the RFC 6455 Sec. 1.3 `Sec-WebSocket-Accept` value for `key`.

    SHA-1 is mandated by the spec for the handshake fingerprint; it is not used
    here as a security primitive.
    """
    return base64.b64encode(
        hashlib.sha1((key + _WS_ACCEPT_GUID).encode()).digest()  # noqa: S324
    ).decode()


def _validate_positive_seconds(value: float | None, name: str) -> None:
    """Reject a non-finite or non-positive duration in seconds.

    `None` disables the feature. NaN fails `> 0` while Infinity passes it but is
    meaningless as a deadline, so `math.isfinite` rejects both.
    """
    if value is not None and not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a finite positive number of seconds, got {value!r}")


def _validate_idle_timeout(idle_timeout: float | None) -> None:
    """Reject a non-finite or non-positive idle timeout."""
    _validate_positive_seconds(idle_timeout, "idle_timeout")


def _validate_heartbeat(heartbeat: float | None) -> None:
    """Reject a non-finite or non-positive heartbeat interval."""
    _validate_positive_seconds(heartbeat, "heartbeat")


# Close codes a peer may send (RFC 6455 Sec. 7.4). The reserved, undefined and
# unassigned ones are a protocol error answered with 1002; >=3000 is taken as given.
_PEER_CLOSE_CODES_OK = frozenset(
    {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014}
)

# RFC 6455 Sec. 5.2 frame opcodes, used by name on the cold paths. The per-frame
# comparisons fold the literal and carry a `# _OP_*` tag, to skip the lookup.
_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

_logger = logging.getLogger(__name__)


#: Bytes unmasked per bignum XOR. Python's big-integer XOR is superlinear in
#: operand size, so one XOR per frame is slower and far hungrier than fixed
#: blocks. Benchmark host, min-of-7:
#:
#:   frame   whole-frame   16 KiB blocks
#:    16 KiB    28.4 us       28.4 us   (identical - below the threshold)
#:    32 KiB    74.3 us       61.0 us   -17.9%
#:    64 KiB   199.7 us      117.1 us   -41.4%
#:     4 MiB    12.1 ms        5.2 ms   -56.7%, peak 4.20x -> 2.00x frame
#:    16 MiB    50.3 ms       22.7 ms   -55.0%, peak 70.5 MB -> 33.6 MB
#:
#: The memory bound is the point: at `MAX_FRAME_SIZE` one masked frame could put
#: ~70 MB of transient bignums on the heap. 8 to 128 KiB measured within ~1%.
_UNMASK_BLOCK = 16 * 1024


def _unmask(payload: bytes, mask: bytes | bytearray, length: int) -> bytes:
    """XOR `payload` with the repeating 4-byte `mask` (RFC 6455 Sec. 5.3)."""
    mask = bytes(mask)
    if length <= _UNMASK_BLOCK:
        tiled = (mask * ((length + 3) // 4))[:length]
        return (int.from_bytes(payload, "big") ^ int.from_bytes(tiled, "big")).to_bytes(
            length, "big"
        )
    # One tiled mask integer per full block; only the ragged tail needs its own.
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

    RFC 6455 Sec. 5.5 caps a control frame at 125 bytes, so the reason is at most
    123 after the 2-byte code; the truncation walks back to a codepoint boundary
    so the frame stays valid UTF-8. Sec. 7.4.1 reserves 1005, 1006 and 1015 for
    local use and forbids them, and anything outside the assigned ranges, from
    appearing on the wire.

    An out-of-range code is coerced rather than raised, because `close` runs on a
    teardown path that suppresses exceptions: a raise would skip the close
    entirely and turn a bad code into the 1006 this exists to avoid.
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


# Terminal sentinel, enqueued to wake a handler parked in `receive_*()`.
_RAW_DISCONNECT: Any = object()


class _Utf8Validator:
    """Incremental UTF-8 validator for streamed TEXT payloads.

    RFC 6455 Sec. 8.1 requires TEXT payloads to be valid UTF-8 and Sec. 5.6 lets
    a message arrive as several continuation fragments, so this remembers the
    partial multi-byte sequence straddling a fragment boundary and rejects a bad
    byte on the first offending fragment rather than at the end.

    A compact form of the Unicode UTF-8 acceptance automaton, so overlong
    encodings, surrogate halves (U+D800-U+DFFF) and values past U+10FFFF are
    rejected without decoding to `str`. `feed` returns False on the first invalid
    byte; `done` is True only at a codepoint boundary.
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

    # One per live connection, so the `__dict__` is the fixed cost at concurrency.
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
        "_send_gate",
        "_pending_pong",
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

    # Kept as a class attribute for compatibility; the paths use `compute_accept`.
    GUID = _WS_ACCEPT_GUID

    # Unbounded, a peer outpacing the handler grows this; at the cap `put` awaits.
    DEFAULT_RECV_QUEUE_MAXSIZE = 64

    # A peer can declare a 64-bit payload length, so uncapped the parser would wait
    # for an arbitrary amount of data. Over it, the connection closes with 1009.
    MAX_FRAME_SIZE = 16 * 1024 * 1024

    # The per-frame cap bounds one frame, but a peer can stream unboundedly many
    # continuation frames, each under it, to grow the reassembly buffer.
    MAX_MESSAGE_SIZE = 16 * 1024 * 1024

    # Declared for the readers that run before `_init_heartbeat` is seen.
    _heartbeat: float | None
    _hb_handle: asyncio.TimerHandle | None
    _hb_token: int | None
    _hb_next_token: int
    _hb_saw_inbound: bool

    # RFC 6455 Sec. 7.1.1 lets an endpoint drop the connection when the peer's
    # close does not arrive in reasonable time.
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
        self._receive_queue: asyncio.Queue[bytes | object] = asyncio.Queue(maxsize=maxsize)

    def _init_common(self) -> None:
        """Set every mode-independent default field shared by both constructors.

        Both constructors call this, so a newly added field is initialised on both
        paths from one place rather than surfacing as an ASGI-only `AttributeError`.
        The per-mode fields stay in the callers: the transport, headers and idle
        timeout, whose static types differ, the receive queue, the heartbeat, the
        ASGI callables, and the scope and path.
        """
        # Stamped by `Veloce._run_websocket`; ASGI's `scope["app"]` is not set.
        self.app: Any = None
        # A declared slot starts unset, so this keeps the `state` property a read.
        self._state: State | None = None
        self._accepted = False
        self._closed = False
        # The peer closed first (RFC 6455 Sec. 5.5.1), so `close()` need not wait.
        self._peer_closed = False
        # Keying the write off this, not `_closed`, is what draws the reply RFC 6455
        # Sec. 5.5.1 wants on a peer-initiated close.
        self._close_frame_sent = False
        # Set by the frame parser on the peer's close; lazy, as ASGI never uses it.
        self._peer_close_event: asyncio.Event | None = None
        # True where `HttpProtocol` wrote the 101, so `accept()` emits no second one.
        self._handshake_sent = False
        # RFC 6455 Sec. 5.4: the opcode being assembled, `None` when none is.
        self._frag_opcode: int | None = None
        self._frag_buffer: bytearray = bytearray()
        self._frag_validator: _Utf8Validator | None = None
        # Kept so a driver reports it even when a vanished peer cancels the close.
        self._handler_exc: BaseException | None = None
        self.close_code: int | None = None
        self.close_reason: str = ""
        self.accepted_subprotocol: str | None = None
        # Byte runs do not line up with frame boundaries, so they accumulate here.
        self._recv_buffer: bytearray = bytearray()
        self._asgi_receive: Any = None
        self._asgi_send: Any = None
        self.scope: dict[str, Any] | None = None
        self.path: str = ""
        self.path_params: dict[str, Any] = {}
        self._query_params: Any = None
        self._cookies: dict[str, str] | None = None
        # Awaited per frame, so a slow reader suspends the producer, not the buffer.
        self._send_drain: Any = None
        # Sync companion for the PONG reply, emitted where no await is possible.
        self._send_gate: Any = None
        # The PING whose PONG was deferred while writing was paused; one at most.
        self._pending_pong: bytes | None = None

    # ── Construction: one object, two transports ──────────────

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

        Headers come from `scope["headers"]`, decoded latin-1 per ASGI, and every
        send, receive and close dispatches through the ASGI callables.

        `idle_timeout` (default `None`, disabled) bounds how long a blocking receive
        waits before a clean `1001 Going Away` close; see the class docstring.
        `heartbeat` is accepted for signature symmetry but inert here, because the
        ASGI server owns ping/pong.
        """
        _validate_idle_timeout(idle_timeout)
        _validate_heartbeat(heartbeat)
        headers: dict[str, str] = {}
        for k, v in scope.get("headers", []):
            headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        ws = cls.__new__(cls)
        ws.transport = None  # type: ignore[assignment]
        ws.headers = headers
        ws._idle_timeout = idle_timeout
        ws._init_common()
        ws._init_heartbeat(None)
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

        The native `HttpProtocol` upgrade path writes the RFC 6455 Sec. 4.2.2 101
        response synchronously, to switch the byte stream, before building this
        object, so `_handshake_sent` is set and a later `accept()` validates state
        without emitting a second handshake. The connection is otherwise a normal
        raw-mode `WebSocket`.

        `headers` are the lowercased handshake headers. `scope` mirrors the ASGI
        websocket shape, so the same accessors work unchanged.
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

        Drives the dispatcher's exit handling. `_closed` alone is the wrong signal in
        raw mode: a peer-initiated close sets it from inside the frame parser before
        the server has sent its reply close frame (RFC 6455 Sec. 5.5.1), so guarding
        on `not _closed` would suppress that reply and hang the handshake. Raw mode
        therefore keys on `_close_frame_sent`; ASGI mode never sends a second close
        after `_closed`, so there `_closed` is the signal.
        """
        if self._is_asgi:
            return not self._closed
        return not self._close_frame_sent

    # ── Introspecting the handshake ───────────────────────────

    def _peek_header(self, name: str) -> str | None:
        """Read one handshake header, as `Request` does.

        The dependency resolver passes a `WebSocket` wherever an HTTP resolve passes
        a `Request`, so anything reading a header off "the connection" - a `Header()`
        parameter, `HTTPBearer`, `HTTPBasic` - lands here and must find both methods.
        """
        return self.headers.get(name.lower())

    def _peek_header_key(self, key: bytes) -> str | None:
        """`_peek_header` for a caller holding the lowercase key as bytes."""
        return self.headers.get(key.decode("latin-1"))

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

        Usage::

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
        # Accepts anything, a missing `Origin` included.
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

    # ── The handshake ─────────────────────────────────────────

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Complete the WebSocket handshake.

        Records the chosen subprotocol on `accepted_subprotocol`.

        `RuntimeError` when the connection is already accepted or already
        closed, and when a `subprotocol` or `headers` argument is passed on the
        native (`Veloce.run`) upgrade path, where the 101 response has already
        been sent and cannot be renegotiated.
        """
        # Accepting twice, or after a close, is a programming error.
        if self._accepted:
            raise RuntimeError("WebSocket.accept(): connection is already accepted")
        if self._closed:
            raise RuntimeError("WebSocket.accept(): connection is already closed")
        # These are written into the 101 response, so CR/LF would inject headers.
        if subprotocol:
            _reject_header_crlf(subprotocol, "WebSocket subprotocol")
        if headers:
            for _k, _v in headers.items():
                _reject_header_crlf(_k, "WebSocket header name")
                _reject_header_crlf(_v, "WebSocket header value")

        if self._is_asgi:
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
            # The 101 is on the wire, so fail loud rather than drop the negotiation.
            if subprotocol is not None or headers is not None:
                raise RuntimeError(
                    "WebSocket.accept(): native (Veloce.run) WebSocket upgrade does not "
                    "support negotiating a subprotocol or custom handshake headers - the 101 "
                    "response was already sent. Run under an ASGI server (uvicorn/hypercorn) "
                    "for native subprotocol negotiation."
                )
            self._accepted = True
            return

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
        # A no-op unless a heartbeat was configured for this connection.
        self.start_heartbeat()

    # ── Sending ───────────────────────────────────────────────

    async def send_text(self, data: str) -> None:
        """Send a text frame."""
        if not self._accepted:
            raise RuntimeError("WebSocket.send_text(): call accept() before sending")
        if self._closed:
            raise WebSocketDisconnect()
        if self._asgi_send is not None:
            # Inlined: this is the frame every message pays.
            try:
                await self._asgi_send({"type": ASGI_EVENT_WS_SEND, "text": data})
            except (ConnectionError, OSError) as exc:
                self._asgi_send_failed(exc)
            return
        await self._raw_send(data.encode("utf-8"), opcode=0x1)  # _OP_TEXT

    async def send_json(self, data: Any, mode: str = "text") -> None:
        """Send JSON data.

        `mode="text"` (default) wraps the JSON in a text frame (opcode 0x1).
        `mode="binary"` sends the raw JSON bytes as a binary frame (0x2).
        """
        if mode not in ("text", "binary"):
            raise ValueError(f"mode must be 'text' or 'binary', got {mode!r}")
        # The shared encoder, so a frame carries the responses' JSON dialect.
        payload = dumps_for(self.app, data)
        if mode == "binary":
            await self.send_bytes(payload)
            return
        if self._asgi_send is not None:
            # The ASGI `websocket.send` event carries a `str`.
            await self.send_text(payload.decode("utf-8"))
            return
        # Routing through `send_text` would transcode the message twice.
        if not self._accepted:
            raise RuntimeError("WebSocket.send_json(): call accept() before sending")
        if self._closed:
            raise WebSocketDisconnect()
        await self._raw_send(payload, opcode=0x1)  # _OP_TEXT

    async def send_bytes(self, data: bytes) -> None:
        """Send a binary frame."""
        if not self._accepted:
            raise RuntimeError("WebSocket.send_bytes(): call accept() before sending")
        if self._closed:
            raise WebSocketDisconnect()
        if self._asgi_send is not None:
            # Inlined: this is the frame every message pays.
            try:
                await self._asgi_send({"type": ASGI_EVENT_WS_SEND, "bytes": data})
            except (ConnectionError, OSError) as exc:
                self._asgi_send_failed(exc)
            return
        await self._raw_send(data, opcode=0x2)  # _OP_BINARY

    async def _asgi_send_safe(self, message: dict[str, Any]) -> None:
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

    def _asgi_disconnected(self, msg: dict[str, Any]) -> NoReturn:
        """Record the peer's close from a `websocket.disconnect` and raise.

        The one place ASGI peer-close is turned into connection state, so the
        unbounded receive path below - which reads the message itself to stay
        one coroutine frame deep - shares this rather than repeating it.
        """
        self._closed = True
        # Through the accessors the raw path populates, so handlers see one API.
        code = msg.get("code", WS_1005_NO_STATUS_RCVD)
        self.close_code = code
        self.close_reason = msg.get("reason", "") or ""
        raise WebSocketDisconnect(code)

    async def _asgi_recv_msg(self) -> dict[str, Any]:
        msg = await self._asgi_receive()
        if msg["type"] == ASGI_EVENT_WS_DISCONNECT:
            self._asgi_disconnected(msg)
        return msg

    async def receive(self) -> dict[str, Any]:
        """Receive a raw ASGI WebSocket message.

        Returns the message dict as the ASGI server delivered it; a
        `websocket.disconnect` raises `WebSocketDisconnect`. ASGI-mode only, since a
        raw connection carries no ASGI envelopes.

        The handshake state machine still applies: receiving before `accept()` would
        consume the `websocket.connect` envelope and corrupt it.
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
            # `_idle_close` always raises, so control never returns here.
            await self._idle_close()

    async def send(self, message: dict[str, Any]) -> None:
        """Send a raw ASGI WebSocket message.

        `message` is forwarded straight to the ASGI `send` callable,
        e.g. `{"type": "websocket.send", "text": "..."}`.
        """
        # The raw escape hatch is not a way around the handshake state machine.
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

    # ── Receiving, drain and the idle timeout ─────────────────

    def _check_can_receive(self, method: str) -> None:
        """Enforce the handshake state machine for receive operations.

        Without them, a receive before `accept()` hangs on an empty queue or on the
        ASGI callable with no clear failure mode. A receive on an already-closed
        connection is a `WebSocketDisconnect`.
        """
        if not self._accepted:
            raise RuntimeError(f"WebSocket.{method}(): call accept() before receiving")
        if self._closed:
            # The recorded code when a close arrived between receives, not 1000.
            raise WebSocketDisconnect(self.close_code or WS_1000_NORMAL_CLOSURE)

    def set_send_gate(self, ready: Any) -> None:
        """Install the sync write-readiness predicate (raw transport only).

        The automatic PONG reply runs inside the synchronous frame parser, which
        cannot await `set_send_drain`'s hook, so `ready()` reports whether the
        transport will accept a write now and the PONG is deferred when it will not.
        """
        self._send_gate = ready

    def flush_pending_pong(self) -> None:
        """Send the deferred PONG, if any, once writing is possible again."""
        payload, self._pending_pong = self._pending_pong, None
        if payload is not None:
            self._send_frame(payload, opcode=0xA)

    def set_send_drain(self, drain: Any) -> None:
        """Install the native write-side backpressure hook (raw transport only).

        `drain` is an awaitable-returning callable that blocks while the transport's
        outgoing buffer is over its high-water mark, so a slow-reading client
        suspends the producing handler instead of letting that buffer grow.
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

        The smaller of the per-call `timeout` and the configured `idle_timeout`;
        either being `None` falls back to the other.
        """
        idle = self._idle_timeout
        if idle is None:
            return timeout
        if timeout is None:
            return idle
        return min(timeout, idle)

    async def _idle_close(self) -> NoReturn:
        """Close cleanly on an idle timeout and signal the handler.

        Closes with `1001 Going Away`, never a fabricated 1006, then raises
        `WebSocketDisconnect` so a receive or `iter_*` loop unwinds as on a peer
        close.
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
            # Taken here, not through the wrappers: with no deadline, nothing to arm.
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

        In raw-transport mode the idle window is measured per completed message, so a
        peer that only streams long fragmented messages or control frames can trip
        it. An idle expiry performs the `1001 Going Away` close; a binding per-call
        `timeout` raises `asyncio.TimeoutError` unchanged.
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
        # The parser's terminal sentinel; a close frame may omit the code.
        if item is _RAW_DISCONNECT:
            raise WebSocketDisconnect(self.close_code or WS_1000_NORMAL_CLOSURE)
        return cast("bytes", item)

    async def _maybe_idle_timeout(self, timeout: float | None, eff: float | None) -> None:
        """Treat a `wait_for` timeout as an idle close when idle won the race.

        A timeout means "idle" only when the effective deadline came from the idle
        window; when the per-call `timeout` was the binding one, the original
        `asyncio.TimeoutError` propagates unchanged.
        """
        if self._idle_timeout is not None and (timeout is None or eff == self._idle_timeout):
            await self._idle_close()

    async def _asgi_recv_text_or_bytes(self, timeout: float | None) -> dict[str, Any]:
        """Receive one ASGI message, bounding the wait only when a deadline is set.

        The common case has neither deadline, so the receive awaits directly and
        skips the `asyncio.wait_for` Task wrapper.
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
            # Taken here, not through the wrappers: with no deadline, nothing to arm.
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

        Usage::

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

    # ── Closing ───────────────────────────────────────────────

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
        # Keyed off `_close_frame_sent`: `_closed` is already set by a peer close.
        if self._close_frame_sent:
            return
        peer_started = self._peer_closed
        self._closed = True
        self._close_frame_sent = True
        self._cancel_heartbeat()
        # Armed first: the parser sets the event only when it is already present.
        if not peer_started:
            self._peer_close_event = asyncio.Event()
        payload = struct.pack("!H", code)
        if reason:
            # `_sanitise_close` already clamped this to the control-frame budget.
            payload += reason.encode("utf-8")
        with contextlib.suppress(Exception):
            self._send_frame(payload, opcode=_OP_CLOSE)
        # Both sides must agree before the socket drops (RFC 6455 Sec. 7.1.1).
        if not peer_started and self._peer_close_event is not None:
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._peer_close_event.wait(), timeout=self.CLOSE_HANDSHAKE_TIMEOUT
                )
        if self.transport is not None:
            self.transport.close()

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
        # A local offset per frame, compacting once at the end: slicing per frame would
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

        # RFC 6455 Sec. 5.2: RSV1-3 (mask 0x70) MUST be zero unless a defining extension
        # was negotiated, and Veloce negotiates none. Rejected before allocation.
        if buf[start] & 0x70:
            self._close_protocol_error()
            return 0

        # RFC 6455 Sec. 5.1: every client-to-server frame MUST be masked, and a server
        # receiving an unmasked one fails the connection with 1002.
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

        # RFC 6455 Sec. 5.5: <=125 bytes and never fragmented, so a violation is 1002
        # rather than, say, an oversized ping echoed back as a pong.
        if opcode in (0x8, 0x9, 0xA) and (payload_len > 125 or not fin):
            self._close_protocol_error()
            return 0

        # Unconditional: every unmasked frame returned above, so re-testing `masked`
        # would leave two always-true branches on the per-frame path.
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
            # Peer-initiated close (RFC 6455 Sec. 5.5.1). `_handle_close_frame` sends the
            # reply, so the dispatcher's clean-exit `close()` emits no second frame.
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
                # RFC 6455 Sec. 5.5.3 permits replying to only the most recent PING. This reply
                # cannot await the drain, so unbounded a peer that stops reading while flooding
                # PINGs grows the write buffer by a PONG apiece.
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
        try:
            self._receive_queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Synchronous: no `await` is available from inside `feed_data`.
            self._close_too_big()

    # ── Heartbeat (raw-transport liveness) ────────────────────
    #
    # A black-holed TCP connection - peer vanished without FIN/RST, common behind
    # NAT - otherwise parks a handler in `receive_*` forever. A tokened PING per
    # window must draw a PONG echoing that token; any inbound byte defers the next
    # probe, and two silent windows mark the peer dead. Inert under ASGI, where the
    # server owns ping/pong.

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

    # ── Context-manager protocol ──────────────────────────────

    async def __aenter__(self) -> WebSocket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        # An exceptional exit leaves closing to the dispatcher, so its mapped code
        # (1008, 1011) is sent: closing here would set `_closed` and skip that.
        if exc[0] is None:
            await self.close()
