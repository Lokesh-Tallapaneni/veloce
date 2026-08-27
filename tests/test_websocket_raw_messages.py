"""WebSocket.receive() / send() raw ASGI message API."""

from __future__ import annotations

from tests._native_ws import mark_accepted
from veloce import Veloce
from veloce.testclient import TestClient
from veloce.websocket import WebSocket


def test_raw_receive_returns_message_dict():
    app = Veloce()
    captured: list[dict] = []

    @app.websocket("/raw")
    async def raw(ws: WebSocket):
        await ws.accept()
        msg = await ws.receive()
        captured.append(msg)
        await ws.send({"type": "websocket.send", "text": "ack"})

    with TestClient(app) as client, client.websocket_connect("/raw") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "ack"

    assert captured[0]["type"] == "websocket.receive"
    assert captured[0].get("text") == "hello"


def test_raw_send_forwards_message():
    app = Veloce()

    @app.websocket("/s")
    async def s(ws: WebSocket):
        await ws.accept()
        await ws.receive()
        await ws.send({"type": "websocket.send", "text": "from-raw-send"})

    with TestClient(app) as client, client.websocket_connect("/s") as ws:
        ws.send_text("x")
        assert ws.receive_text() == "from-raw-send"


async def test_raw_receive_non_asgi_raises():
    # A transport-mode WebSocket has no ASGI receive callable.
    ws = WebSocket(transport=None, headers={})
    # Skip the handshake — `receive()` now enforces accept-before-receive,
    # which would fire first. This test pins the *other* check
    # (transport-mode → no ASGI escape hatch).
    mark_accepted(ws)

    with __import__("pytest").raises(RuntimeError, match="ASGI-mode only"):
        await ws.receive()
