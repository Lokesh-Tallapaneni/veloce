"""app.add_websocket_route + websocket_route — ASGI shape."""

from __future__ import annotations

from veloce import Veloce
from veloce.testclient import TestClient
from veloce.websocket import WebSocket


def test_add_websocket_route_imperative():
    app = Veloce()

    async def echo(ws: WebSocket):
        await ws.accept()
        async for msg in ws.iter_text():
            await ws.send_text(f"echo:{msg}")

    app.add_websocket_route("/ws", echo)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "echo:hi"


def test_websocket_route_decorator_alias():
    app = Veloce()

    @app.websocket_route("/chat")
    async def chat(ws: WebSocket):
        await ws.accept()
        async for msg in ws.iter_text():
            await ws.send_text(msg.upper())

    with TestClient(app) as client, client.websocket_connect("/chat") as ws:
        ws.send_text("loud")
        assert ws.receive_text() == "LOUD"


def test_websocket_route_is_websocket_alias():
    assert Veloce.websocket_route is Veloce.websocket


def test_websocket_exposes_the_application():
    # `ws.app` mirrors `request.app` so a handler reaches app state directly;
    # the ASGI `scope` carries no `app` key, so this is the supported accessor.

    app = Veloce(openapi_url=None)
    app.state.marker = "value"
    seen = {}

    @app.websocket("/ws")
    async def handler(ws: WebSocket) -> None:
        seen["is_app"] = ws.app is app
        seen["state"] = ws.app.state.marker
        seen["scope_has_app"] = "app" in ws.scope
        await ws.accept()
        await ws.close()

    with app.test_client().websocket_connect("/ws"):
        pass

    assert seen["is_app"] is True
    assert seen["state"] == "value"
    assert seen["scope_has_app"] is False
