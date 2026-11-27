"""WebSocket async-context-manager protocol (`async with ws:`)."""

from __future__ import annotations

import pytest

from tests._ws_drive import run_ws
from veloce import Veloce, WebSocket, WebSocketException
from veloce.websocket import WebSocket as RawWebSocket

_WS_CLOSE = "websocket.close"


def _asgi_ws() -> tuple[RawWebSocket, list[dict]]:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "websocket.disconnect", "code": 1000}

    ws = RawWebSocket.from_asgi({"type": "websocket", "path": "/ws"}, receive, send)
    return ws, sent


async def test_async_with_closes_on_normal_exit():
    ws, sent = _asgi_ws()
    async with ws as entered:
        assert entered is ws
    assert sent[-1]["type"] == _WS_CLOSE
    assert sent[-1]["code"] == 1000


async def test_async_with_does_not_close_on_exception():
    # On exception, `__aexit__` must NOT send its own normal-closure frame;
    # closing is deferred to the dispatcher so the mapped error code wins.
    ws, sent = _asgi_ws()
    with pytest.raises(ValueError):
        async with ws:
            raise ValueError("boom")
    assert not any(m["type"] == _WS_CLOSE for m in sent)
    assert not ws._closed


async def test_async_with_close_is_idempotent():
    ws, sent = _asgi_ws()
    async with ws:
        await ws.close()
    # `close()` is a no-op once already closed, so only one close event.
    assert sum(m["type"] == _WS_CLOSE for m in sent) == 1


def test_async_with_preserves_dispatcher_close_code():
    # Regression: a handler using `async with ws:` that raises
    # WebSocketException must still close with the exception's code (1008),
    # not a normal-closure 1000 from `__aexit__`.
    app = Veloce()

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        async with ws:
            await ws.accept()
            raise WebSocketException(1008, "nope")

    sent = run_ws(app, "/ws")
    close = [m for m in sent if m["type"] == _WS_CLOSE][0]
    assert close["code"] == 1008
    assert close["reason"] == "nope"


def test_async_with_maps_unhandled_error_to_1011():
    # An unhandled error closes with 1011 before the dispatcher re-raises it
    # for logging / teardown - `__aexit__` must not pre-empt that with 1000.
    app = Veloce()

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        async with ws:
            await ws.accept()
            raise RuntimeError("boom")

    sent = run_ws(app, "/ws", raises=RuntimeError)
    close = [m for m in sent if m["type"] == _WS_CLOSE][0]
    assert close["code"] == 1011
