"""WebSocket support — basic implementation over raw asyncio."""

from __future__ import annotations

import asyncio
import base64
import enum
import hashlib
import struct
from typing import Any

import orjson

from veloce.exceptions import WebSocketDisconnect
from veloce.http.response import _reject_header_crlf


class WebSocketState(enum.IntEnum):
    """Connection-state enum — ASGI shape.

    `CONNECTING` is the initial state before `accept()` has been sent
    (client side) or received (application side). `CONNECTED` once the
    handshake completes; `DISCONNECTED` once a close frame has been
    sent or received on the corresponding side.
    """

    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2


class WebSocket:
    """WebSocket connection handler."""

    GUID = "258EAFA5-E914-47DA-95CA-5AB5DC525D63"

    def __init__(self, transport: asyncio.Transport, headers: dict[str, str]) -> None:
        self.transport = transport
        self.headers = headers
        self._accepted = False
        self._closed = False
        self._receive_queue: asyncio.Queue[bytes] = asyncio.Queue()
        # Fragmented-message reassembly state (RFC 6455 §5.4). `_frag_opcode`
        # is the data opcode of the message currently being assembled, or
        # `None` when no fragmented message is in progress.
        self._frag_opcode: int | None = None
        self._frag_buffer: bytearray = bytearray()
        # ASGI mode (W1). When wired through `Veloce.__call__`'s websocket
        # branch, the transport is None and we drive the connection through
        # ASGI receive/send callables instead. Set by `from_asgi`.
        self._asgi_receive: Any = None
        self._asgi_send: Any = None
        self.scope: dict | None = None
        self.path: str = ""
        self.path_params: dict[str, Any] = {}

    @classmethod
    def from_asgi(
        cls,
        scope: dict,
        receive: Any,
        send: Any,
    ) -> WebSocket:
        """Construct an ASGI-driven WebSocket (no asyncio.Transport).

        Used by `Veloce.__call__` for `scope["type"] == "websocket"`.
        Headers come from `scope["headers"]` (list of `(bytes, bytes)`),
        decoded latin-1 per ASGI. `accept`/`send_*`/`receive_*`/`close`
        all dispatch through `send`/`receive` instead of the raw frame
        writer used by the asyncio.Transport mode.
        """
        headers: dict[str, str] = {}
        for k, v in scope.get("headers", []):
            headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        # `cls.__new__` to skip the transport-required __init__.
        ws = cls.__new__(cls)
        ws.transport = None  # type: ignore[assignment]
        ws.headers = headers
        ws._accepted = False
        ws._closed = False
        ws._receive_queue = asyncio.Queue()  # unused in ASGI mode
        ws._frag_opcode = None  # unused in ASGI mode (no raw frame parsing)
        ws._frag_buffer = bytearray()
        ws._asgi_receive = receive
        ws._asgi_send = send
        ws.scope = scope
        ws.path = scope.get("path", "")
        ws.path_params = {}
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
        from veloce.http.datastructures import QueryParams

        raw = b""
        if self.scope:
            raw = self.scope.get("query_string", b"") or b""
        qs = raw.decode("latin-1") if isinstance(raw, bytes) else str(raw)
        return QueryParams.from_query_string(qs)

    @property
    def url(self) -> str:
        """The WebSocket handshake URL path — ASGI-style shape.

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
        from veloce.http.request import Address

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
        from veloce.http.request import State

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
        from veloce.http.cookies import parse_cookie

        return parse_cookie(self.headers.get("cookie", ""))

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
    def requested_subprotocols(self) -> list[str]:
        """Subprotocols the client offered in `Sec-WebSocket-Protocol`.

        Returns them in client preference order (RFC 6455 §1.9). Empty
        list when the header is absent. Whitespace around each token is
        stripped; the comparison the negotiator performs is case-sensitive
        per the spec.
        """
        raw = self.headers.get("sec-websocket-protocol", "")
        if not raw:
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    def negotiate_subprotocol(self, supported: list[str]) -> str | None:
        """Pick the first client-offered subprotocol that the server supports.

        Per RFC 6455 §4.1, the server picks ONE protocol from the client's
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
        # or already-closed connection is a programming error — surface it
        # as a clear exception rather than re-running the handshake.
        if self._accepted:
            raise RuntimeError("WebSocket.accept(): connection is already accepted")
        if self._closed:
            raise RuntimeError("WebSocket.accept(): connection is already closed")
        # Reject CR/LF in the negotiated subprotocol and any custom
        # handshake headers — they are written into the 101 response.
        if subprotocol:
            _reject_header_crlf(subprotocol, "WebSocket subprotocol")
        if headers:
            for _k, _v in headers.items():
                _reject_header_crlf(_k, "WebSocket header name")
                _reject_header_crlf(_v, "WebSocket header value")

        if self._is_asgi:
            # ASGI: consume the connect message, then emit accept.
            msg = await self._asgi_receive()
            if msg["type"] != "websocket.connect":
                raise RuntimeError(f"expected websocket.connect, got {msg['type']!r}")
            accept_msg: dict[str, Any] = {"type": "websocket.accept"}
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
        key = self.headers.get("sec-websocket-key", "")
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
            await self._asgi_send({"type": "websocket.send", "text": data})
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
            await self._asgi_send({"type": "websocket.send", "bytes": data})
            return
        self._send_frame(data, opcode=0x2)

    async def _asgi_recv_msg(self) -> dict:
        msg = await self._asgi_receive()
        if msg["type"] == "websocket.disconnect":
            self._closed = True
            raise WebSocketDisconnect()
        return msg

    async def receive(self) -> dict:
        """Receive a raw ASGI WebSocket message.

        Returns the message dict as the ASGI server delivered it
        (`{"type": "websocket.receive", "text"/"bytes": ...}`). A
        `websocket.disconnect` message raises `WebSocketDisconnect`.
        ASGI-mode only — raw asyncio-transport connections don't carry
        ASGI message envelopes.
        """
        if not self._is_asgi:
            raise RuntimeError(
                "WebSocket.receive() is ASGI-mode only; use receive_text/"
                "receive_bytes for raw asyncio-transport connections"
            )
        return await self._asgi_recv_msg()

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

    async def receive_text(self, timeout: float | None = None) -> str:
        """Receive a text message. Raises asyncio.TimeoutError if timeout exceeded."""
        if self._is_asgi:
            msg = await asyncio.wait_for(self._asgi_recv_msg(), timeout=timeout)
            data = msg.get("text") or (msg.get("bytes") or b"").decode("utf-8")
            return data
        data = await asyncio.wait_for(self._receive_queue.get(), timeout=timeout)
        return data.decode("utf-8") if isinstance(data, bytes) else str(data)

    async def receive_json(self, timeout: float | None = None) -> Any:
        """Receive and parse JSON."""
        text = await self.receive_text(timeout=timeout)
        return orjson.loads(text)

    async def receive_bytes(self, timeout: float | None = None) -> bytes:
        """Receive binary data. Raises asyncio.TimeoutError if timeout exceeded."""
        if self._is_asgi:
            msg = await asyncio.wait_for(self._asgi_recv_msg(), timeout=timeout)
            return msg.get("bytes") or msg.get("text", "").encode("utf-8")
        return await asyncio.wait_for(self._receive_queue.get(), timeout=timeout)

    async def iter_text(self) -> Any:
        """Async-iterate over incoming text frames until the peer closes.

        Usage:
            async for msg in ws.iter_text():
                ...

        Terminates cleanly on `WebSocketDisconnect`. Other exceptions
        propagate.
        """
        from veloce.exceptions import WebSocketDisconnect

        try:
            while True:
                yield await self.receive_text()
        except WebSocketDisconnect:
            return

    async def iter_bytes(self) -> Any:
        """Async-iterate over incoming binary frames until the peer closes."""
        from veloce.exceptions import WebSocketDisconnect

        try:
            while True:
                yield await self.receive_bytes()
        except WebSocketDisconnect:
            return

    async def iter_json(self) -> Any:
        """Async-iterate over incoming JSON-decoded frames until peer closes."""
        from veloce.exceptions import WebSocketDisconnect

        try:
            while True:
                yield await self.receive_json()
        except WebSocketDisconnect:
            return

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame.

        Per RFC 6455 §5.5.1 the close-frame payload is a 2-byte big-endian
        status code optionally followed by a UTF-8 reason of at most
        123 bytes (so the whole payload fits in the 125-byte
        control-frame budget). Reasons longer than 123 bytes are
        truncated to a clean UTF-8 boundary.
        """
        if self._closed:
            return
        self._closed = True
        if self._is_asgi:
            await self._asgi_send({"type": "websocket.close", "code": code, "reason": reason or ""})
            return
        payload = struct.pack("!H", code)
        if reason:
            reason_bytes = reason.encode("utf-8")[:123]
            # Walk back from a 123-byte truncation if the byte boundary
            # landed mid-codepoint — keeps the close-frame valid UTF-8.
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
        """Feed one raw WebSocket frame from the transport (called by the
        protocol).

        Handles fragmented messages (RFC 6455 §5.4): a data frame with
        `FIN=0` opens a message that subsequent continuation frames
        (opcode `0x0`) extend, and the `FIN=1` continuation completes it.
        Control frames (close / ping / pong) are never fragmented and may
        be interleaved within a fragmented message without disturbing the
        reassembly buffer.
        """
        # One frame is assumed to arrive per call — a frame split across
        # transport reads is dropped by the length guards below.
        if len(data) < 2:
            return

        fin = bool(data[0] & 0x80)
        opcode = data[0] & 0x0F
        masked = bool(data[1] & 0x80)
        payload_len = data[1] & 0x7F

        offset = 2
        if payload_len == 126:
            if len(data) < 4:
                return
            payload_len = struct.unpack("!H", data[2:4])[0]
            offset = 4
        elif payload_len == 127:
            if len(data) < 10:
                return
            payload_len = struct.unpack("!Q", data[2:10])[0]
            offset = 10

        if masked:
            if len(data) < offset + 4:
                return
            mask = data[offset : offset + 4]
            offset += 4

        if len(data) < offset + payload_len:
            return

        payload = bytearray(data[offset : offset + payload_len])
        if masked:
            for i in range(len(payload)):
                payload[i] ^= mask[i % 4]

        # Control frames (close / ping / pong) — never fragmented; handled
        # independently of any fragmented message in progress.
        if opcode == 0x8:  # Close
            self._closed = True
            raise WebSocketDisconnect()
        if opcode == 0x9:  # Ping
            self._send_frame(bytes(payload), opcode=0xA)  # Pong
            return
        if opcode == 0xA:  # Pong — no application-level action.
            return

        # Data frames (text / binary) and continuation frames.
        if opcode in (0x1, 0x2):
            # A data frame must not arrive mid-fragmentation — RFC 6455
            # §5.4 allows only continuation frames after the opening
            # frame. If a peer sends one anyway, discard the abandoned
            # partial and clear the reassembly state cleanly so a later
            # continuation cannot append to a stale buffer.
            if fin:
                # Unfragmented message — deliver immediately.
                self._frag_opcode = None
                self._frag_buffer = bytearray()
                self._receive_queue.put_nowait(bytes(payload))
            else:
                # Opening frame of a fragmented message — start buffering
                # (supersedes any abandoned partial).
                self._frag_opcode = opcode
                self._frag_buffer = bytearray(payload)
        elif opcode == 0x0:  # Continuation frame.
            if self._frag_opcode is None:
                # A continuation with no message in progress is a protocol
                # error — drop the stray frame rather than corrupt state.
                return
            self._frag_buffer += payload
            if fin:
                # Final fragment — the reassembled message is complete.
                self._receive_queue.put_nowait(bytes(self._frag_buffer))
                self._frag_opcode = None
                self._frag_buffer = bytearray()

    def _send_frame(self, data: bytes, opcode: int) -> None:
        """Send a WebSocket frame."""
        frame = bytearray()
        frame.append(0x80 | opcode)  # FIN + opcode

        length = len(data)
        if length < 126:
            frame.append(length)
        elif length < 65536:
            frame.append(126)
            frame.extend(struct.pack("!H", length))
        else:
            frame.append(127)
            frame.extend(struct.pack("!Q", length))

        frame.extend(data)
        self.transport.write(bytes(frame))
