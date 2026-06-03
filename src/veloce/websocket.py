"""WebSocket support - basic implementation over raw asyncio."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import enum
import hashlib
import math
import struct
from typing import TYPE_CHECKING, Any, NoReturn

import orjson

from veloce._constants import (
    HEADER_COOKIE,
    HEADER_ORIGIN,
    HEADER_SEC_WEBSOCKET_KEY,
    HEADER_SEC_WEBSOCKET_PROTOCOL,
)
from veloce._internal import _reject_header_crlf
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
    WS_1009_MESSAGE_TOO_BIG,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable


def _validate_idle_timeout(idle_timeout: float | None) -> None:
    """Reject a non-finite or non-positive idle timeout.

    Mirrors `EventSourceResponse.ping` validation: `None` disables the
    feature, otherwise the value must be a finite positive number of
    seconds. NaN fails `> 0` and Infinity passes `> 0` but is meaningless
    as a deadline, so both are rejected via `math.isfinite`.
    """
    if idle_timeout is not None and not (math.isfinite(idle_timeout) and idle_timeout > 0):
        raise ValueError(
            f"idle_timeout must be a finite positive number of seconds, got {idle_timeout!r}"
        )


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
    """

    # RFC 6455 Sec. 1.3 magic GUID, concatenated with the client's
    # `Sec-WebSocket-Key` and SHA-1+base64'd to form `Sec-WebSocket-Accept`.
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

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

    def __init__(
        self,
        transport: asyncio.Transport,
        headers: dict[str, str],
        recv_queue_maxsize: int | None = None,
        idle_timeout: float | None = None,
    ) -> None:
        _validate_idle_timeout(idle_timeout)
        self.transport = transport
        self.headers = headers
        self._accepted = False
        self._closed = False
        self._idle_timeout = idle_timeout
        maxsize = (
            recv_queue_maxsize
            if recv_queue_maxsize is not None
            else self.DEFAULT_RECV_QUEUE_MAXSIZE
        )
        self._receive_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=maxsize)
        # Fragmented-message reassembly state (RFC 6455 Sec. 5.4). `_frag_opcode`
        # is the data opcode of the message currently being assembled, or
        # `None` when no fragmented message is in progress.
        self._frag_opcode: int | None = None
        self._frag_buffer: bytearray = bytearray()
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
        self.scope: dict | None = None
        self.path: str = ""
        self.path_params: dict[str, Any] = {}
        self._query_params: Any = None
        self._cookies: dict[str, str] | None = None

    @classmethod
    def from_asgi(
        cls,
        scope: dict,
        receive: Any,
        send: Any,
        idle_timeout: float | None = None,
    ) -> WebSocket:
        """Construct an ASGI-driven WebSocket (no asyncio.Transport).

        Used by `Veloce.__call__` for `scope["type"] == "websocket"`.
        Headers come from `scope["headers"]` (list of `(bytes, bytes)`),
        decoded latin-1 per ASGI. `accept`/`send_*`/`receive_*`/`close`
        all dispatch through `send`/`receive` instead of the raw frame
        writer used by the asyncio.Transport mode.

        `idle_timeout` (default `None` -> disabled) bounds how long a
        blocking receive waits for the next frame before performing a
        clean `1001 Going Away` close; see the class docstring.
        """
        _validate_idle_timeout(idle_timeout)
        headers: dict[str, str] = {}
        for k, v in scope.get("headers", []):
            headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        # `cls.__new__` to skip the transport-required __init__.
        ws = cls.__new__(cls)
        ws.transport = None  # type: ignore[assignment]
        ws.headers = headers
        ws._accepted = False
        ws._closed = False
        ws._idle_timeout = idle_timeout
        ws._receive_queue = None  # type: ignore[assignment]
        ws._frag_opcode = None  # unused in ASGI mode (no raw frame parsing)
        ws._frag_buffer = bytearray()
        ws._recv_buffer = bytearray()  # unused in ASGI mode (no raw frame parsing)
        ws._asgi_receive = receive
        ws._asgi_send = send
        ws.scope = scope
        ws.path = scope.get("path", "")
        ws.path_params = {}
        ws._query_params = None
        ws._cookies = None
        return ws

    @property
    def _is_asgi(self) -> bool:
        return self._asgi_send is not None

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
        existing = getattr(self, "_state", None)
        if existing is None:
            existing = State()
            object.__setattr__(self, "_state", existing)
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
        """Complete the WebSocket handshake."""
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
            self._accepted = True
            return

        # Raw-transport mode (HTTP/1.1 101 handshake).
        key = self.headers.get(HEADER_SEC_WEBSOCKET_KEY.lower(), "")
        accept_key = base64.b64encode(
            hashlib.sha1((key + self.GUID).encode()).digest()  # noqa: S324
        ).decode()

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
        self._accepted = True

    async def send_text(self, data: str) -> None:
        """Send a text frame."""
        if not self._accepted:
            raise RuntimeError("WebSocket.send_text(): call accept() before sending")
        if self._closed:
            raise WebSocketDisconnect()
        if self._is_asgi:
            await self._asgi_send({"type": ASGI_EVENT_WS_SEND, "text": data})
            return
        self._send_frame(data.encode("utf-8"), opcode=0x1)

    async def send_json(self, data: Any, mode: str = "text") -> None:
        """Send JSON data.

        `mode="text"` (default) wraps the JSON in a text frame (opcode 0x1).
        `mode="binary"` sends the raw JSON bytes as a binary frame (0x2).
        """
        if mode not in ("text", "binary"):
            raise ValueError(f"mode must be 'text' or 'binary', got {mode!r}")
        payload = orjson.dumps(data)
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
        if self._is_asgi:
            await self._asgi_send({"type": ASGI_EVENT_WS_SEND, "bytes": data})
            return
        self._send_frame(data, opcode=0x2)

    async def _asgi_recv_msg(self) -> dict:
        msg = await self._asgi_receive()
        if msg["type"] == ASGI_EVENT_WS_DISCONNECT:
            self._closed = True
            raise WebSocketDisconnect()
        return msg

    async def receive(self) -> dict:
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

    async def send(self, message: dict) -> None:
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
        await self._asgi_send(message)

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
            raise WebSocketDisconnect()

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
        if self._is_asgi:
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
            return await self._receive_queue.get()
        try:
            return await asyncio.wait_for(self._receive_queue.get(), timeout=eff)
        except (TimeoutError, asyncio.TimeoutError):
            await self._maybe_idle_timeout(timeout, eff)
            raise

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
        if self._is_asgi:
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
        """Send a close frame.

        Per RFC 6455 Sec. 5.5.1 the close-frame payload is a 2-byte big-endian
        status code optionally followed by a UTF-8 reason of at most
        123 bytes (so the whole payload fits in the 125-byte
        control-frame budget). Reasons longer than 123 bytes are
        truncated to a clean UTF-8 boundary.
        """
        if self._closed:
            return
        self._closed = True
        if self._is_asgi:
            await self._asgi_send(
                {"type": ASGI_EVENT_WS_CLOSE, "code": code, "reason": reason or ""}
            )
            return
        payload = struct.pack("!H", code)
        if reason:
            reason_bytes = reason.encode("utf-8")[:123]
            # Walk back from a 123-byte truncation if the byte boundary
            # landed mid-codepoint - keeps the close-frame valid UTF-8.
            while reason_bytes:
                try:
                    reason_bytes.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    reason_bytes = reason_bytes[:-1]
            payload += reason_bytes
        self._send_frame(payload, opcode=0x8)
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

        payload_bytes = bytes(buf[start + offset : start + offset + payload_len])
        if masked and payload_len:
            # Bulk XOR via Python's bignum int. Tile the 4-byte mask to
            # the payload length and XOR in a single C-level op - far
            # cheaper than a Python-level per-byte loop for any frame
            # past a handful of bytes (and WebSocket frames are usually
            # hundreds to KiB-sized).
            tiled = (bytes(mask) * ((payload_len + 3) // 4))[:payload_len]
            payload_bytes = (
                int.from_bytes(payload_bytes, "big") ^ int.from_bytes(tiled, "big")
            ).to_bytes(payload_len, "big")
        payload = bytearray(payload_bytes)

        # Control frames (close / ping / pong) - never fragmented; handled
        # independently of any fragmented message in progress.
        if opcode == 0x8:  # Close
            self._closed = True
            raise WebSocketDisconnect()
        if opcode == 0x9:  # Ping
            self._send_frame(bytes(payload), opcode=0xA)  # Pong
            return frame_len
        if opcode == 0xA:  # Pong - no application-level action.
            return frame_len

        # Data frames (text / binary) and continuation frames.
        if opcode in (0x1, 0x2):
            # A data frame must not arrive mid-fragmentation - RFC 6455
            # Sec. 5.4 allows only continuation frames after the opening
            # frame. If a peer sends one anyway, discard the abandoned
            # partial and clear the reassembly state cleanly so a later
            # continuation cannot append to a stale buffer.
            if fin:
                # Unfragmented message - deliver immediately.
                self._frag_opcode = None
                self._frag_buffer = bytearray()
                self._enqueue_or_close(bytes(payload))
            else:
                # Opening frame of a fragmented message - start buffering
                # (supersedes any abandoned partial).
                self._frag_opcode = opcode
                self._frag_buffer = bytearray(payload)
                if len(self._frag_buffer) > self.MAX_MESSAGE_SIZE:
                    self._close_too_big()
                    return 0
        elif opcode == 0x0:  # Continuation frame.
            if self._frag_opcode is None:
                # A continuation with no message in progress is a protocol
                # error - drop the stray frame rather than corrupt state.
                return frame_len
            self._frag_buffer += payload
            # Cap the cumulative reassembled size: the per-frame cap bounds
            # one frame, but a stream of continuation frames could otherwise
            # grow the buffer without limit (unbounded-allocation DoS).
            if len(self._frag_buffer) > self.MAX_MESSAGE_SIZE:
                self._close_too_big()
                return 0
            if fin:
                # Final fragment - the reassembled message is complete.
                self._enqueue_or_close(bytes(self._frag_buffer))
                self._frag_opcode = None
                self._frag_buffer = bytearray()

        # The frame was fully consumed regardless of opcode-specific
        # handling - report its length so the caller drops it and looks
        # for the next frame in the buffer.
        return frame_len

    def _close_too_big(self) -> None:
        """Close the connection with `1009 Message Too Big`.

        Used when a peer declares a frame payload past `MAX_FRAME_SIZE`.
        Mirrors the synchronous close in `_enqueue_or_close` - no `await`
        is available from inside the Protocol callback that drives
        `feed_data`.
        """
        with contextlib.suppress(Exception):
            self._send_frame((WS_1009_MESSAGE_TOO_BIG).to_bytes(2, "big"), opcode=0x8)  # Close
        self._closed = True
        with contextlib.suppress(Exception):
            if self.transport is not None:
                self.transport.close()

    def _close_protocol_error(self) -> None:
        """Close the connection with `1002 Protocol Error`.

        Used for malformed frames - e.g. an oversized (>125 byte) or
        fragmented control frame (RFC 6455 Sec. 5.5). Like `_close_too_big`,
        the close is synchronous: no `await` is available from inside the
        Protocol callback that drives `feed_data`.
        """
        with contextlib.suppress(Exception):
            self._send_frame((WS_1002_PROTOCOL_ERROR).to_bytes(2, "big"), opcode=0x8)  # Close
        self._closed = True
        with contextlib.suppress(Exception):
            if self.transport is not None:
                self.transport.close()

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
