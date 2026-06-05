"""Native (raw-transport) WebSocket dispatch through `Veloce._run_websocket`.

The ASGI branch of `Veloce.__call__` and the raw-transport serving path share
one dispatch core: `Veloce._run_websocket(ws, route_info)`. It runs accept-time
DI through the shared `DependencyResolver`, calls the handler, maps the exit to a
close code (validation -> 1008, `WebSocketException` -> its own code,
`WebSocketDisconnect` handled as a clean unwind, generic -> 1011 + re-raise,
clean -> 1000), and drains `yield`-style teardowns exception-aware.

These tests drive that core over a genuine `asyncio.Transport`-backed
`WebSocket` (the native mode, `transport is not None`) rather than the ASGI
receive/send envelope, so they exercise the same helper the native upgrade
handler calls while holding only the app reference (no app-level import in
`serving/`). The end-to-end cases stand up a real localhost asyncio server and
connect with a raw-socket RFC 6455 client (a genuine HTTP/1.1 upgrade handshake
plus masked client frames) so a complete native connection flows through
`_run_websocket`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import struct

import pytest

from veloce import Veloce, WebSocket, status
from veloce._protocol_constants import ROUTE_METHOD_WEBSOCKET
from veloce.dependency import Depends
from veloce.exceptions import WebSocketException, WebSocketRequestValidationError

_RFC6455_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _FakeTransport(asyncio.Transport):
    """Minimal raw transport that records frames and the close call."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def writelines(self, data) -> None:
        # A real transport coalesces the buffers on the wire; join them into a
        # single recorded write so frame headers and payloads stay contiguous.
        self.writes.append(b"".join(bytes(chunk) for chunk in data))

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


def _client_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Build one masked client->server frame (RFC 6455 Sec. 5)."""
    mask = b"\x12\x34\x56\x78"
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    b0 = (0x80 if fin else 0x00) | opcode
    n = len(payload)
    if n < 126:
        header = bytes([b0, 0x80 | n])
    elif n < 65536:
        header = bytes([b0, 0x80 | 126]) + struct.pack("!H", n)
    else:
        header = bytes([b0, 0x80 | 127]) + struct.pack("!Q", n)
    return header + mask + masked


def _make_native_ws() -> tuple[WebSocket, _FakeTransport]:
    transport = _FakeTransport()
    ws = WebSocket(transport, {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="})
    return ws, transport


def _close_code(transport: _FakeTransport) -> int | None:
    """Decode the status code from the last close frame written, if any.

    Server->client frames are unmasked, so a close frame is exactly `0x88`
    (FIN + close opcode) followed by a length byte (< 126 for a status code)
    and the 2-byte big-endian code. The 101 handshake response is written as
    HTTP text and is skipped.
    """
    for raw in reversed(transport.writes):
        if len(raw) >= 4 and raw[0] == 0x88 and (raw[1] & 0x80) == 0:
            payload = raw[2 : 2 + (raw[1] & 0x7F)]
            if len(payload) >= 2:
                return struct.unpack("!H", payload[:2])[0]
    return None


def _match(app: Veloce, path: str):
    m = app.match(ROUTE_METHOD_WEBSOCKET, path)
    assert m is not None
    return m.route_info, m.path_params


# ── Close-code mapping on the native (raw-transport) path ────────────


async def test_native_clean_close_sends_1000():
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()

    ws, transport = _make_native_ws()
    route_info, ws.path_params = _match(app, "/ws")
    await app._run_websocket(ws, route_info)

    assert ws._closed
    assert _close_code(transport) == status.WS_1000_NORMAL_CLOSURE


async def test_native_websocket_exception_uses_its_code():
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        raise WebSocketException(code=status.WS_1003_UNSUPPORTED_DATA, reason="nope")

    ws, transport = _make_native_ws()
    route_info, ws.path_params = _match(app, "/ws")
    # Swallowed - an application-driven close is not an error.
    await app._run_websocket(ws, route_info)

    assert ws._closed
    assert _close_code(transport) == status.WS_1003_UNSUPPORTED_DATA


async def test_native_generic_exception_closes_1011_and_reraises():
    app = Veloce(openapi_url=None)
    boom = RuntimeError("handler blew up")

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        raise boom

    ws, transport = _make_native_ws()
    route_info, ws.path_params = _match(app, "/ws")

    # The native driver (the dispatch task) is expected to catch the re-raise
    # and log it, mirroring how an unhandled handler exception surfaces today.
    with pytest.raises(RuntimeError, match="handler blew up"):
        await app._run_websocket(ws, route_info)

    assert ws._closed
    assert _close_code(transport) == status.WS_1011_INTERNAL_ERROR


async def test_native_validation_error_closes_1008():
    app = Veloce(openapi_url=None)

    def _bad_dep() -> str:
        raise WebSocketRequestValidationError([{"loc": ["x"], "msg": "bad"}])

    @app.websocket("/ws")
    async def handler(ws: WebSocket, dep: str = Depends(_bad_dep)):
        await ws.accept()  # never reached

    ws, transport = _make_native_ws()
    route_info, ws.path_params = _match(app, "/ws")
    # Validation failure is swallowed and mapped to 1008.
    await app._run_websocket(ws, route_info)

    assert ws._closed
    assert _close_code(transport) == status.WS_1008_POLICY_VIOLATION


async def test_native_handler_close_is_not_overridden():
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        await ws.close(code=status.WS_1001_GOING_AWAY)

    ws, transport = _make_native_ws()
    route_info, ws.path_params = _match(app, "/ws")
    await app._run_websocket(ws, route_info)

    # The handler already closed; the dispatcher's clean-close branch must not
    # send a second close frame.
    assert _close_code(transport) == status.WS_1001_GOING_AWAY
    close_frames = [w for w in transport.writes if w and w[0] == 0x88]
    assert len(close_frames) == 1


# ── Dependency teardown ordering / exception-awareness ──────────────


async def test_native_teardown_runs_after_clean_close():
    app = Veloce(openapi_url=None)
    events: list[str] = []

    def dep_a():
        events.append("a-setup")
        yield "a"
        events.append("a-teardown")

    def dep_b():
        events.append("b-setup")
        yield "b"
        events.append("b-teardown")

    @app.websocket("/ws")
    async def handler(ws: WebSocket, a: str = Depends(dep_a), b: str = Depends(dep_b)):
        await ws.accept()
        events.append("handler")

    ws, _ = _make_native_ws()
    route_info, ws.path_params = _match(app, "/ws")
    await app._run_websocket(ws, route_info)

    # Teardowns drain in reverse registration order (ExitStack semantics).
    assert events == [
        "a-setup",
        "b-setup",
        "handler",
        "b-teardown",
        "a-teardown",
    ]


async def test_native_teardown_sees_handler_exception():
    app = Veloce(openapi_url=None)
    seen: list[BaseException | None] = []

    def dep():
        try:
            yield "v"
        except RuntimeError as exc:
            seen.append(exc)
            raise

    @app.websocket("/ws")
    async def handler(ws: WebSocket, v: str = Depends(dep)):
        await ws.accept()
        raise RuntimeError("boom")

    ws, _ = _make_native_ws()
    route_info, ws.path_params = _match(app, "/ws")
    with pytest.raises(RuntimeError, match="boom"):
        await app._run_websocket(ws, route_info)

    # The generator was thrown into with the live exception (exception-aware
    # teardown), not advanced with a plain `next`.
    assert len(seen) == 1
    assert str(seen[0]) == "boom"


# ── Real localhost server + raw-socket RFC 6455 client end-to-end ───


class _NativeWSServerProtocol(asyncio.Protocol):
    """Drives one raw-transport WebSocket connection through `_run_websocket`.

    A genuine HTTP/1.1 upgrade: the handshake request is parsed off the wire,
    a raw-transport `WebSocket` is built, the route is matched, and the shared
    dispatch core runs while inbound bytes are pumped into `ws.feed_data`. This
    is the same call the native upgrade handler makes, using only the held app
    reference - no app-level import in the serving layer.
    """

    def __init__(self, app: Veloce) -> None:
        self.app = app
        self.transport: asyncio.Transport | None = None
        self.ws: WebSocket | None = None
        self._buffer = bytearray()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def data_received(self, data: bytes) -> None:
        if self.ws is not None:
            self.ws.feed_data(data)
            return
        self._buffer += data
        if b"\r\n\r\n" not in self._buffer:
            return
        head, _, rest = self._buffer.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        path = lines[0].split(" ", 2)[1]
        headers: dict[str, str] = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
        assert self.transport is not None
        ws = WebSocket(self.transport, headers)
        ws.path = path
        self.ws = ws
        if rest:
            ws.feed_data(bytes(rest))
        match = self.app.match(ROUTE_METHOD_WEBSOCKET, path)
        if match is None:
            self.transport.close()
            return
        ws.path_params = match.path_params
        asyncio.ensure_future(self._dispatch(ws, match.route_info))

    async def _dispatch(self, ws: WebSocket, route_info) -> None:
        try:
            await self.app._run_websocket(ws, route_info)
        except Exception:
            # Mirror the native dispatch task: an unhandled handler exception
            # was already mapped to a 1011 close inside `_run_websocket`; the
            # driver just drops the transport.
            if self.transport is not None:
                self.transport.close()


class _RawWSClient:
    """A minimal RFC 6455 client over a real socket - genuine handshake + frames.

    A purpose-built client is used instead of a third-party one so the test
    pins Veloce's own framing against the spec (correct accept-key GUID, masked
    client->server frames, server->client text frames) without depending on an
    external library's wire behaviour.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(cls, host: str, port: int, path: str) -> _RawWSClient:
        reader, writer = await asyncio.open_connection(host, port)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(request.encode())
        await writer.drain()
        # Read and validate the 101 handshake response.
        head = await reader.readuntil(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        assert "101" in status_line, status_line
        resp_headers = {}
        for line in header_lines:
            k, _, v = line.partition(":")
            if k:
                resp_headers[k.strip().lower()] = v.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + _RFC6455_GUID).encode()).digest()  # noqa: S324
        ).decode()
        assert resp_headers.get("sec-websocket-accept") == expected
        return cls(reader, writer)

    async def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        header = bytes([0x81, 0x80 | len(payload)])  # FIN + text, masked
        self._writer.write(header + mask + masked)
        await self._writer.drain()

    async def recv_text(self) -> str:
        b0 = await self._reader.readexactly(1)
        assert b0[0] & 0x0F == 0x1, "expected a text frame"
        b1 = await self._reader.readexactly(1)
        length = b1[0] & 0x7F
        if length == 126:
            length = struct.unpack("!H", await self._reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._reader.readexactly(8))[0]
        payload = await self._reader.readexactly(length)
        return payload.decode("utf-8")

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


async def test_native_real_localhost_echo_roundtrip():
    app = Veloce(openapi_url=None)

    @app.websocket("/echo")
    async def echo(ws: WebSocket):
        await ws.accept()
        async for message in ws.iter_text():
            await ws.send_text(f"echo:{message}")

    loop = asyncio.get_running_loop()
    server = await loop.create_server(lambda: _NativeWSServerProtocol(app), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client = await _RawWSClient.connect("127.0.0.1", port, "/echo")
        try:
            await client.send_text("hello")
            assert await client.recv_text() == "echo:hello"
            await client.send_text("world")
            assert await client.recv_text() == "echo:world"
        finally:
            await client.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_native_real_localhost_di_value_injected():
    app = Veloce(openapi_url=None)

    def greeting() -> str:
        return "hi"

    @app.websocket("/greet")
    async def greet(ws: WebSocket, msg: str = Depends(greeting)):
        await ws.accept()
        await ws.send_text(msg)

    loop = asyncio.get_running_loop()
    server = await loop.create_server(lambda: _NativeWSServerProtocol(app), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client = await _RawWSClient.connect("127.0.0.1", port, "/greet")
        try:
            assert await client.recv_text() == "hi"
        finally:
            await client.close()
    finally:
        server.close()
        await server.wait_closed()
