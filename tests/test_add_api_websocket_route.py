"""app.add_api_websocket_route — the imperative imperative WS registration."""

from __future__ import annotations

from veloce import Veloce
from veloce.testclient import TestClient
from veloce.websocket import WebSocket


def test_add_api_websocket_route_registers_handler():
    app = Veloce()

    async def echo(ws: WebSocket):
        await ws.accept()
        msg = await ws.receive_text()
        await ws.send_text(f"echo:{msg}")

    app.add_api_websocket_route("/ws", echo)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"


def test_add_api_websocket_route_accepts_name():
    app = Veloce()

    async def handler(ws: WebSocket):
        await ws.accept()
        await ws.close()

    # `name` registers the route for reverse lookup.
    app.add_api_websocket_route("/ws", handler, name="ws_endpoint")
    assert app.url_for("ws_endpoint") == "/ws"

    with TestClient(app) as client, client.websocket_connect("/ws"):
        pass


def test_path_params_resolved():
    app = Veloce()

    async def room(ws: WebSocket, room_id: str):
        await ws.accept()
        await ws.send_text(room_id)

    app.add_api_websocket_route("/room/{room_id}", room)

    with TestClient(app) as client, client.websocket_connect("/room/lobby") as ws:
        assert ws.receive_text() == "lobby"
