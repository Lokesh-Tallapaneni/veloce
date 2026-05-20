"""W1: ASGI websocket scope wiring + TestClient.websocket_connect."""

from __future__ import annotations

import pytest

from veloce import Veloce


def _make_app() -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    return app


def test_websocket_echo_text_via_test_client():
    app = _make_app()

    @app.websocket("/ws")
    async def echo(ws):
        await ws.accept()
        msg = await ws.receive_text()
        await ws.send_text(f"echo: {msg}")
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "echo: hello"


def test_websocket_subprotocol_negotiation():
    app = _make_app()

    @app.websocket("/ws")
    async def neg(ws):
        chosen = ws.negotiate_subprotocol(["chat-v1", "chat-v2"])
        await ws.accept(subprotocol=chosen)
        await ws.send_text(chosen or "")
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws", subprotocols=["chat-v2", "chat-v1"]) as ws:
        assert ws.accepted_subprotocol == "chat-v2"
        assert ws.receive_text() == "chat-v2"


def test_websocket_json_round_trip():
    app = _make_app()

    @app.websocket("/ws")
    async def js(ws):
        await ws.accept()
        data = await ws.receive_json()
        await ws.send_json({"echoed": data})
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as ws:
        ws.send_text('{"hi": 1}')
        assert ws.receive_json() == {"echoed": {"hi": 1}}


def test_websocket_unknown_path_rejects():
    """No registered handler → ASGI close with code 1008."""
    app = _make_app()
    client = app.test_client()
    with (
        pytest.raises(RuntimeError, match="rejected with close code 1008"),
        client.websocket_connect("/missing"),
    ):
        pass
