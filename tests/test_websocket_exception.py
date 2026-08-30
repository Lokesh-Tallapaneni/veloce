"""WebSocketException — application-driven WebSocket close."""

from __future__ import annotations

import asyncio

from veloce import Veloce, WebSocket, WebSocketException


def _run_ws(app: Veloce, path: str) -> list[dict]:
    """Drive one WebSocket connection through the ASGI surface, returning
    every message the app sent."""
    scope = {"type": "websocket", "path": path, "headers": [], "query_string": b""}
    incoming = [{"type": "websocket.connect"}]
    sent: list[dict] = []

    async def receive() -> dict:
        if incoming:
            return incoming.pop(0)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict) -> None:
        sent.append(message)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app(scope, receive, send))
    finally:
        loop.close()
    return sent


def test_exception_constructs_with_code_and_reason():
    exc = WebSocketException(1008, "policy")
    assert exc.code == 1008
    assert exc.reason == "policy"


def test_exception_reason_optional():
    exc = WebSocketException(1011)
    assert exc.code == 1011
    assert exc.reason is None


def test_raised_after_accept_closes_with_code():
    app = Veloce()

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        raise WebSocketException(1008, "nope")

    sent = _run_ws(app, "/ws")
    close = [m for m in sent if m["type"] == "websocket.close"][0]
    assert close["code"] == 1008
    assert close["reason"] == "nope"


def test_raised_before_accept_closes_connection():
    app = Veloce()

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        raise WebSocketException(1003, "unsupported")

    sent = _run_ws(app, "/ws")
    close = [m for m in sent if m["type"] == "websocket.close"][0]
    assert close["code"] == 1003


def test_exception_is_swallowed_not_propagated():
    app = Veloce()

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        raise WebSocketException(1008)

    # No exception escapes the dispatch — the call completes cleanly.
    sent = _run_ws(app, "/ws")
    assert any(m["type"] == "websocket.close" for m in sent)


def test_no_reason_sends_empty_string():
    app = Veloce()

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        raise WebSocketException(1011)

    sent = _run_ws(app, "/ws")
    close = [m for m in sent if m["type"] == "websocket.close"][0]
    assert close["code"] == 1011
    assert close.get("reason", "") == ""


def test_websocket_exception_is_importable_from_package_root():
    # The import *is* the assertion - the test is named for it, and moving
    # it to module top would move the failure to collection.
    from veloce import WebSocketDisconnect, WebSocketException

    assert issubclass(WebSocketException, Exception)
    assert issubclass(WebSocketDisconnect, Exception)
