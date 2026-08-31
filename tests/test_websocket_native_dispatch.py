"""Native (raw-transport) WebSocket dispatch through `Veloce._run_websocket`.

The ASGI branch of `Veloce.__call__` and the raw-transport serving path share
one dispatch core: `Veloce._run_websocket(ws, route_info)`. It runs accept-time
DI through the shared `DependencyResolver`, calls the handler, maps the exit to a
close code (validation -> 1008, `WebSocketException` -> its own code,
`WebSocketDisconnect` handled as a clean unwind, generic -> 1011 + re-raise,
clean -> 1000), and drains `yield`-style teardowns exception-aware.

These tests drive that core over a genuine `asyncio.Transport`-backed
`WebSocket` (the native mode, `transport is not None`) rather than the ASGI
receive/send envelope, so the close-code mapping is asserted against the frames
that actually reach a transport.

Over a real socket the core is reached through `HttpProtocol`, in
`test_websocket_native_server.py`. This module used to stand up its own
localhost server driven by a test-owned protocol class - sixty lines
re-implementing what `HttpProtocol` does, which proved the re-implementation
worked rather than the framework. Its one unduplicated case, `Depends()` over a
real socket, moved to the production path as
`test_native_upgrade_injects_a_dependency`.
"""

from __future__ import annotations

import struct

import pytest

from tests._protocol import _FakeTransport
from veloce import Veloce, WebSocket, status
from veloce._protocol_constants import ROUTE_METHOD_WEBSOCKET
from veloce.dependency import Depends
from veloce.exceptions import WebSocketException, WebSocketRequestValidationError


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
