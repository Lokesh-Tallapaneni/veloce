"""Declarative WebSocket listener — accept/receive-loop/close via a callback."""

from __future__ import annotations

import pytest

from veloce import Veloce, WebSocket
from veloce.testclient import TestClient


def test_listener_echo_json_round_trip():
    app = Veloce()

    @app.websocket_listener("/echo")
    async def echo(data):
        return {"echo": data}

    with TestClient(app) as client, client.websocket_connect("/echo") as ws:
        ws.send_json({"x": 1})
        assert ws.receive_json() == {"echo": {"x": 1}}
        ws.send_json([1, 2, 3])
        assert ws.receive_json() == {"echo": [1, 2, 3]}


def test_listener_text_mode():
    app = Veloce()

    @app.websocket_listener("/echo", receive="text", send="text")
    async def echo(data):
        return f"got:{data}"

    with TestClient(app) as client, client.websocket_connect("/echo") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "got:hi"


def test_listener_bytes_mode():
    app = Veloce()

    @app.websocket_listener("/rev", receive="bytes", send="bytes")
    async def rev(data):
        return data[::-1]

    with TestClient(app) as client, client.websocket_connect("/rev") as ws:
        ws.send_bytes(b"abc")
        assert ws.receive_bytes() == b"cba"


def test_listener_none_return_sends_nothing():
    """A consumer that returns None emits no frame; later real sends still flow."""
    app = Veloce()
    seen: list = []

    @app.websocket_listener("/sink", receive="text", send="text")
    async def sink(data):
        seen.append(data)
        if data == "ping":
            return "pong"
        return None

    with TestClient(app) as client, client.websocket_connect("/sink") as ws:
        ws.send_text("a")
        ws.send_text("b")
        ws.send_text("ping")
        # Only the "ping" produced a frame; "a"/"b" were consumed silently.
        assert ws.receive_text() == "pong"
    assert seen == ["a", "b", "ping"]


def test_listener_disconnect_runs_hooks_and_terminates():
    app = Veloce()
    events: list[str] = []

    async def on_connect(ws: WebSocket):
        events.append("connect")

    async def on_disconnect(ws: WebSocket):
        events.append("disconnect")

    @app.websocket_listener(
        "/h", receive="text", send="text", on_connect=on_connect, on_disconnect=on_disconnect
    )
    async def h(data):
        return data

    with TestClient(app) as client, client.websocket_connect("/h") as ws:
        ws.send_text("x")
        assert ws.receive_text() == "x"
    # Context exit sent websocket.disconnect; the loop ended and on_disconnect ran.
    assert events == ["connect", "disconnect"]


def test_listener_callback_receives_socket_when_declared():
    app = Veloce()

    @app.websocket_listener("/ws", receive="text", send="text")
    async def handler(ws, data):
        assert isinstance(ws, WebSocket)
        return f"{ws.path}:{data}"

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text("m")
        assert ws.receive_text() == "/ws:m"


def test_listener_sync_callback_offloaded():
    app = Veloce()

    @app.websocket_listener("/sync", receive="text", send="text")
    def echo(data):  # plain sync def
        return data.upper()

    with TestClient(app) as client, client.websocket_connect("/sync") as ws:
        ws.send_text("abc")
        assert ws.receive_text() == "ABC"


def test_listener_disconnect_hook_runs_on_immediate_close():
    app = Veloce()
    events: list[str] = []

    async def on_disconnect(ws: WebSocket):
        events.append("disconnect")

    @app.websocket_listener("/h", receive="text", on_disconnect=on_disconnect)
    async def h(data):
        return None

    with TestClient(app) as client, client.websocket_connect("/h"):
        pass  # close right away, no messages sent
    assert events == ["disconnect"]


def test_listener_invalid_mode_rejected():
    app = Veloce()

    with pytest.raises(ValueError, match="receive mode"):

        @app.websocket_listener("/x", receive="bogus")
        async def h(data):
            return data

    with pytest.raises(ValueError, match="send mode"):

        @app.websocket_listener("/y", send="bogus")
        async def h2(data):
            return data


def test_sync_listener_callback_sees_app_context():
    """A sync `websocket_listener` callback offloaded to a worker thread still
    sees ContextVar-backed helpers like `current_app` (it runs under a copied
    context, matching sync HTTP handlers)."""
    from veloce import current_app

    app = Veloce()
    app.config["MARK"] = "ok"

    @app.websocket_listener("/ctx", receive="text", send="text")
    def on_receive(data):  # sync callback -> offloaded to a thread
        # Would raise "Working outside of application context" if the context
        # were lost across the executor boundary.
        return f"{data}:{current_app.config['MARK']}"

    with TestClient(app) as client, client.websocket_connect("/ctx") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "hi:ok"
