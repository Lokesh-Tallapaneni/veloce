"""The `WebSocket` connection object — state, handshake, send, receive and close."""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import struct
from typing import TYPE_CHECKING, Any, NoReturn, cast

import veloce._json as _json
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
    WS_1005_NO_STATUS_RCVD,
    WS_1006_ABNORMAL_CLOSURE,
)
from veloce.websocket.close import _sanitise_close
from veloce.websocket.frames import (
    _OP_CLOSE,
    _RAW_DISCONNECT,
)
from veloce.websocket.handshake import (
    _WS_ACCEPT_GUID,
    _validate_heartbeat,
    _validate_idle_timeout,
    compute_accept,
)
from veloce.websocket.raw import RawTransportMixin
from veloce.websocket.utf8 import _Utf8Validator

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable


_logger = logging.getLogger(__name__)


class WebSocketState(enum.IntEnum):
    """Connection-state enum - ASGI shape.

    `CONNECTING` until `accept()` is sent (client side) or received
    (application side), `CONNECTED` once the handshake completes, and
    `DISCONNECTED` once a close frame crosses that side.
    """

    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2


class WebSocket(RawTransportMixin):
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

    ``async with ws:`` closes the connection with a normal-closure 1000 on a
    clean exit. If the block exits via an exception, ``__aexit__`` leaves the
    close to the dispatcher's error handling, which sends the mapped close code
    (1008 for a policy violation, 1011 for an unhandled error) first.

    Pass ``idle_timeout=<seconds>`` (default ``None`` -> disabled) to bound how
    long a blocking receive or ``iter_*`` loop waits for the next message. On
    expiry the connection performs a clean RFC 6455 close with
    ``1001 Going Away`` and the receive raises ``WebSocketDisconnect``, so the
    handler unwinds as it would on a peer-initiated close. A per-call
    ``timeout`` still applies and the smaller deadline wins. Set it via
    ``from_asgi(idle_timeout=...)`` or, inside the handler,
    ``set_idle_timeout``. The window bounds each complete message.

    Pass ``heartbeat=<seconds>`` (raw transport only, default ``None`` ->
    disabled) to probe a silent peer: after ``accept()`` a timer sends a tokened
    application PING every ``heartbeat`` seconds, and a peer that answers with
    neither a PONG nor any other frame before the next tick is dropped with
    ``1006`` recorded on ``ws.close_code``. Any inbound byte defers the next
    probe. Accepted for symmetry in ASGI mode, which owns ping/pong, but starts
    no timer there.
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

    # A peer may declare a 64-bit length, so uncapped the parser would wait for
    # arbitrarily many bytes. Over this, the connection closes with 1009.
    MAX_FRAME_SIZE = 16 * 1024 * 1024

    # The per-frame cap bounds one frame; a peer could still stream unboundedly
    # many continuations, each under it, to grow the reassembly buffer.
    MAX_MESSAGE_SIZE = 16 * 1024 * 1024

    # Declared for the readers that run before `_init_heartbeat` is seen.
    _heartbeat: float | None
    _hb_handle: asyncio.TimerHandle | None
    _hb_token: int | None
    _hb_next_token: int
    _hb_saw_inbound: bool

    # RFC 6455 Sec. 7.1.1 permits dropping a peer whose close never arrives.
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
        self._receive_queue: asyncio.Queue[bytes | object] | None = asyncio.Queue(maxsize=maxsize)

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
        # Keyed off this, not `_closed`, to draw the reply RFC 6455 Sec. 5.5.1
        # wants on a peer-initiated close.
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
        ws.transport = None
        ws.headers = headers
        ws._idle_timeout = idle_timeout
        ws._init_common()
        ws._init_heartbeat(None)
        ws._receive_queue = None
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

        Pass a single origin string or an iterable of them. Normalisation
        matches `WebSocketOriginMiddleware` - each side lowercased and stripped
        of a trailing slash - so an allow-list written for one works with the
        other.

        - **Wildcard.** `"*"` in `allowed` accepts any origin, the opt-in "I
          have my own check elsewhere" escape hatch.
        - **Missing `Origin`** (absent, or a literal `Origin: null` from a
          sandboxed iframe or `file://` page) returns `False`. Non-browser
          clients legitimately omit it; branch on `ws.origin is None` to allow
          them, or use `WebSocketOriginMiddleware`'s `allow_missing=True`.

        Usage::

            @app.websocket("/ws")
            async def chat(ws: WebSocket):
                if not ws.check_origin("https://app.example.com"):
                    await ws.close(code=WS_1008_POLICY_VIOLATION)  # policy violation
                    return
                await ws.accept()
                ...

        For a check registered once, before the handler, reach for
        `WebSocketOriginMiddleware`.
        """
        allowed_set = frozenset((allowed,)) if isinstance(allowed, str) else frozenset(allowed)
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
        queue = self._receive_queue
        assert queue is not None  # raw transport only; the ASGI path never reaches here
        if eff is None:
            item = await queue.get()
        else:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=eff)
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
        return _json.loads(text)

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

    # ── Context-manager protocol ──────────────────────────────

    async def __aenter__(self) -> WebSocket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        # An exceptional exit leaves closing to the dispatcher, so its mapped code
        # (1008, 1011) is sent: closing here would set `_closed` and skip that.
        if exc[0] is None:
            await self.close()
