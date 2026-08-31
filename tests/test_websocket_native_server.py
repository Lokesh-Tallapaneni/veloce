"""Native WebSocket upgrade through `HttpProtocol` (the `Veloce.run` serving path).

These tests exercise the real raw-transport serving protocol end to end: a
genuine RFC 6455 HTTP/1.1 upgrade handshake is performed against a localhost
`asyncio` server whose connections are driven by `veloce.serving.protocol.
HttpProtocol`, exactly as `Veloce.run()` wires it. A purpose-built raw-socket
client does the handshake (correct accept-key GUID, masked client->server
frames) and the server's own framing answers it, so the whole native path -
upgrade detection, 101, divert, `feed_data`, `_run_websocket` dispatch, close -
is covered without any third-party WebSocket library.

The handshake-refusal cases (no matching route, wrong version, bad Origin)
assert the protocol replies with a plain HTTP response and never a 101.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os

import pytest

from tests._raw_ws_client import RawWSClient
from veloce import Veloce, WebSocket, current_app, status
from veloce.dependency import Depends
from veloce.helpers import g
from veloce.middleware.security import (
    TrustedHostMiddleware,
    WebSocketOriginMiddleware,
)
from veloce.serving.protocol import HttpProtocol


async def _start_server(app: Veloce) -> tuple[asyncio.AbstractServer, int]:
    loop = asyncio.get_running_loop()
    server = await loop.create_server(lambda: HttpProtocol(app, loop), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


@contextlib.asynccontextmanager
async def _serving(app: Veloce):
    """Serve `app` on an ephemeral port; close and drain it on the way out.

    `server.close()` + `await server.wait_closed()` in a `finally` was written
    out in all seventeen tests - a teardown the eighteenth would have to
    remember, and one that leaks a listening socket for the rest of the session
    when it is forgotten. Two tests restore an exception handler as well and
    keep their own `finally` for that.
    """
    server, port = await _start_server(app)
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


# ── Successful handshake + echo ─────────────────────────────────────


async def test_native_upgrade_echo_roundtrip():
    app = Veloce(openapi_url=None)

    @app.websocket("/echo")
    async def echo(ws: WebSocket):
        await ws.accept()
        async for message in ws.iter_text():
            await ws.send_text(f"echo:{message}")

    async with _serving(app) as port:
        client = await RawWSClient.connect("127.0.0.1", port, "/echo")
        client.assert_accepted()
        try:
            await client.send_text("hello")
            assert await client.recv_text() == "echo:hello"
            await client.send_text("world")
            assert await client.recv_text() == "echo:world"
        finally:
            await client.close()


async def test_native_upgrade_path_params_reach_the_handler():
    app = Veloce(openapi_url=None)

    @app.websocket("/room/{name}")
    async def room(ws: WebSocket, name: str):
        await ws.accept()
        await ws.send_text(name)

    async with _serving(app) as port:
        client = await RawWSClient.connect("127.0.0.1", port, "/room/lobby")
        client.assert_accepted()
        try:
            assert await client.recv_text() == "lobby"
        finally:
            await client.close()


async def test_native_upgrade_injects_a_dependency():
    """`Depends()` is resolved at accept time over a real native socket.

    Accept-time DI runs inside the shared `_run_websocket` core, but reaching it
    through `HttpProtocol` is what proves the upgrade path hands the resolver a
    usable connection - not a test-owned driver standing in for the protocol.
    """
    app = Veloce(openapi_url=None)

    def greeting() -> str:
        return "hi"

    @app.websocket("/greet")
    async def greet(ws: WebSocket, msg: str = Depends(greeting)):
        await ws.accept()
        await ws.send_text(msg)

    async with _serving(app) as port:
        client = await RawWSClient.connect("127.0.0.1", port, "/greet")
        client.assert_accepted()
        try:
            assert await client.recv_text() == "hi"
        finally:
            await client.close()


async def test_native_upgrade_query_string_visible():
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        await ws.send_text(ws.query_params.get("token", ""))

    async with _serving(app) as port:
        client = await RawWSClient.connect("127.0.0.1", port, "/ws?token=abc123")
        client.assert_accepted()
        try:
            assert await client.recv_text() == "abc123"
        finally:
            await client.close()


async def test_native_peer_close_unwinds_handler():
    app = Veloce(openapi_url=None)
    finished = asyncio.Event()

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        try:
            async for _ in ws.iter_text():
                pass
        finally:
            finished.set()

    async with _serving(app) as port:
        client = await RawWSClient.connect("127.0.0.1", port, "/ws")
        client.assert_accepted()
        await client.send_close()
        await client.close()
        await asyncio.wait_for(finished.wait(), timeout=2.0)


async def test_native_peer_close_completes_handshake_without_tcp_drop():
    # Regression: a peer that sends a close frame and keeps the TCP socket open
    # awaiting the server's reply must wake the parked receiver, unwind the
    # handler, and elicit the server's own close frame (RFC 6455 Sec. 5.5.1).
    # Before the fix the handler stayed blocked on `_receive_queue.get()` and
    # the server never replied, hanging until the client hard-closed the socket.
    app = Veloce(openapi_url=None)
    finished = asyncio.Event()

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        try:
            async for _ in ws.iter_text():
                pass
        finally:
            finished.set()

    async with _serving(app) as port:
        client = await RawWSClient.connect("127.0.0.1", port, "/ws")
        client.assert_accepted()
        # Send a normal close and keep the TCP socket open.
        await client.send_close(code=status.WS_1000_NORMAL_CLOSURE)
        # The server must reply with its own close frame promptly, not hang.
        reply_code = await asyncio.wait_for(client.recv_close(), timeout=2.0)
        assert reply_code == status.WS_1000_NORMAL_CLOSURE
        # And the handler must have unwound rather than stayed blocked.
        await asyncio.wait_for(finished.wait(), timeout=2.0)
        await client.close()


async def test_native_server_initiated_close_sends_frame():
    # The handler initiates the close; the server sends a close frame and the
    # peer (still open) receives it. The bounded handshake wait must not hang
    # the dispatch when the peer replies.
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        await ws.send_text("bye")
        await ws.close(code=status.WS_1001_GOING_AWAY)

    async with _serving(app) as port:
        client = await RawWSClient.connect("127.0.0.1", port, "/ws")
        client.assert_accepted()
        assert await client.recv_text() == "bye"
        reply_code = await asyncio.wait_for(client.recv_close(), timeout=2.0)
        assert reply_code == status.WS_1001_GOING_AWAY
        # Complete the handshake from the client side so the server's bounded
        # wait resolves without tripping its timeout.
        await client.send_close(code=status.WS_1001_GOING_AWAY)
        await client.close()


async def test_native_handler_has_app_context():
    # Regression: native WebSocket dispatch must bind `current_app` / `g` the
    # same way the ASGI path does, so handlers and helpers that read them work
    # under `Veloce.run()` instead of raising "Working outside of application
    # context".

    app = Veloce(openapi_url=None)
    app.config["WS_CONTEXT_MARKER"] = "bound"

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        g.marker = "set"
        # Reading `current_app` and `g` would raise "Working outside of
        # application context" if the native dispatch failed to bind them.
        marker = current_app.config["WS_CONTEXT_MARKER"]
        await ws.send_text(f"{marker}:{g.marker}")
        await ws.close()

    async with _serving(app) as port:
        client = await RawWSClient.connect("127.0.0.1", port, "/ws")
        client.assert_accepted()
        assert await client.recv_text() == "bound:set"
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.recv_close(), timeout=2.0)
        await client.send_close()
        await client.close()


async def test_native_upgrade_no_loop_exception_on_connect():
    # Regression: httptools raises `HttpParserUpgrade` after a successful
    # divert. `data_received` must suppress it so the event loop's exception
    # handler is never invoked - otherwise every successful connect logs a
    # spurious traceback.
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        await ws.send_text("ok")
        await ws.close()

    loop = asyncio.get_running_loop()
    seen: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: seen.append(context))
    server, port = await _start_server(app)
    try:
        client = await RawWSClient.connect("127.0.0.1", port, "/ws")
        client.assert_accepted()
        assert await client.recv_text() == "ok"
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.recv_close(), timeout=2.0)
        await client.send_close()
        await client.close()
        # Let any queued exception-handler callbacks run.
        await asyncio.sleep(0)
        upgrade_errors = [ctx for ctx in seen if isinstance(ctx.get("exception"), Exception)]
        assert not upgrade_errors, upgrade_errors
    finally:
        loop.set_exception_handler(previous)
        server.close()
        await server.wait_closed()


async def test_native_upgrade_first_frame_in_handshake_segment():
    # Regression: a client that pipelines its first masked frame into the same
    # TCP segment as the handshake. httptools raises `HttpParserUpgrade` at the
    # body offset, so the post-handshake bytes (the frame) never reach the HTTP
    # parser. `data_received` must feed `data[offset:]` to the frame parser or
    # the first message is silently dropped and the connection hangs.
    app = Veloce(openapi_url=None)

    @app.websocket("/echo")
    async def echo(ws: WebSocket):
        await ws.accept()
        async for message in ws.iter_text():
            await ws.send_text(f"echo:{message}")

    server, port = await _start_server(app)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            "GET /echo HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: keep-alive, Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        # The first text frame, masked, glued onto the handshake in one write.
        payload = b"hello"
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x81, 0x80 | len(payload)]) + mask + masked
        writer.write(handshake + frame)
        await writer.drain()

        head = await reader.readuntil(b"\r\n\r\n")
        assert b"101" in head.split(b"\r\n", 1)[0]

        # The echo must come back from the frame sent in the handshake segment.
        # Without the fix the frame is dropped, so this read times out.
        b0 = await asyncio.wait_for(reader.readexactly(1), timeout=2.0)
        assert b0[0] & 0x0F == 0x1
        length = (await reader.readexactly(1))[0] & 0x7F
        body = await reader.readexactly(length)
        assert body == b"echo:hello"
    finally:
        # Close the client first so a parked handler unwinds, then bound the
        # server teardown so a no-fix run fails on the assertion above rather
        # than hanging the suite.
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        server.close()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(server.wait_closed(), timeout=2.0)


# ── Handshake refusals (plain HTTP, never a 101) ────────────────────


async def test_native_upgrade_no_route_returns_404():
    app = Veloce(openapi_url=None)

    async with _serving(app) as port:
        client = await RawWSClient.connect("127.0.0.1", port, "/nope")
        assert "404" in client.status_line
        assert "101" not in client.status_line
        await client.close()


async def test_native_upgrade_wrong_version_returns_426():
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()

    async with _serving(app) as port:
        client = await RawWSClient.connect("127.0.0.1", port, "/ws", version="8")
        assert "426" in client.status_line
        assert client.resp_headers.get("sec-websocket-version") == "13"
        await client.close()


async def test_native_upgrade_bad_origin_returns_403():
    app = Veloce(openapi_url=None)
    app.add_middleware(WebSocketOriginMiddleware, allowed_origins=["https://good.example.com"])

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()

    async with _serving(app) as port:
        client = await RawWSClient.connect(
            "127.0.0.1", port, "/ws", origin="https://evil.example.com"
        )
        assert "403" in client.status_line
        assert "101" not in client.status_line
        await client.close()


async def test_native_upgrade_good_origin_accepts():
    app = Veloce(openapi_url=None)
    app.add_middleware(WebSocketOriginMiddleware, allowed_origins=["https://good.example.com"])

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        await ws.send_text("ok")

    async with _serving(app) as port:
        client = await RawWSClient.connect(
            "127.0.0.1", port, "/ws", origin="https://good.example.com"
        )
        client.assert_accepted()
        try:
            assert await client.recv_text() == "ok"
        finally:
            await client.close()


async def test_native_upgrade_bad_host_returns_403():
    app = Veloce(openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["allowed.example.com"])

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()

    async with _serving(app) as port:
        client = await RawWSClient.connect("127.0.0.1", port, "/ws", host_header="evil.example.com")
        assert "403" in client.status_line
        assert "101" not in client.status_line
        await client.close()


async def test_native_h2c_upgrade_returns_400_and_handler_does_not_run():
    # Regression: an `Upgrade: h2c` (or any non-WebSocket upgrade) request is a
    # protocol this server does not speak. The client must receive a 400 and the
    # matching route handler must NOT execute - dispatching it would commit side
    # effects for a request the client is told failed, and retries would
    # double-execute.
    app = Veloce(openapi_url=None)
    ran = asyncio.Event()

    @app.get("/upgrade")
    async def upgrade_handler():
        ran.set()
        return {"ok": True}

    async with _serving(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"GET /upgrade HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Connection: Upgrade\r\n"
            b"Upgrade: h2c\r\n\r\n"
        )
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), timeout=2.0)
        assert b"400" in data.split(b"\r\n", 1)[0]
        # Give any erroneously-scheduled dispatch a chance to run.
        await asyncio.sleep(0.05)
        assert not ran.is_set(), "handler ran for a request the client saw fail"
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


# ── A plain HTTP request on the same protocol is unaffected ─────────


async def test_plain_http_get_not_treated_as_upgrade():
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    async with _serving(app) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await writer.drain()
        data = await reader.read()
        assert b"200" in data.split(b"\r\n", 1)[0]
        assert b'{"ok":true}' in data
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


# ── accept() refuses native subprotocol / header negotiation ────────


async def test_native_accept_subprotocol_raises():
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        # The 101 was already sent by the protocol, so a subprotocol cannot be
        # negotiated here - accept() must fail loud.
        with pytest.raises(RuntimeError, match="native"):
            await ws.accept(subprotocol="chat")
        await ws.close()

    async with _serving(app) as port:
        client = await RawWSClient.connect("127.0.0.1", port, "/ws")
        client.assert_accepted()
        try:
            opcode, _ = await client.recv_frame()
            assert opcode == 0x8  # the handler closed after the failed accept
        finally:
            await client.close()
