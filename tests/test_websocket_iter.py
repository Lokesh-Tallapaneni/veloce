"""WebSocket iter_text / iter_bytes / iter_json — ASGI shape."""

from __future__ import annotations

from veloce import Veloce
from veloce.testclient import TestClient
from veloce.websocket import WebSocket


def test_iter_text_yields_messages_until_disconnect():
    app = Veloce()

    @app.websocket("/echo")
    async def echo(ws: WebSocket):
        await ws.accept()
        async for m in ws.iter_text():
            await ws.send_text(f"got:{m}")

    with TestClient(app) as client, client.websocket_connect("/echo") as ws:
        ws.send_text("a")
        assert ws.receive_text() == "got:a"
        ws.send_text("b")
        assert ws.receive_text() == "got:b"
        # Context exit triggers websocket.disconnect; handler's
        # iter_text loop terminates cleanly without raising.


def test_iter_json_yields_decoded_objects():
    app = Veloce()

    @app.websocket("/json")
    async def h(ws: WebSocket):
        await ws.accept()
        async for obj in ws.iter_json():
            await ws.send_json({"echo": obj})

    with TestClient(app) as client, client.websocket_connect("/json") as ws:
        ws.send_json({"x": 1})
        assert ws.receive_json() == {"echo": {"x": 1}}
        ws.send_json([1, 2, 3])
        assert ws.receive_json() == {"echo": [1, 2, 3]}


def test_iter_bytes_yields_frames():
    app = Veloce()

    @app.websocket("/b")
    async def h(ws: WebSocket):
        await ws.accept()
        async for chunk in ws.iter_bytes():
            await ws.send_bytes(chunk[::-1])

    with TestClient(app) as client, client.websocket_connect("/b") as ws:
        ws.send_bytes(b"abc")
        assert ws.receive_bytes() == b"cba"


def test_iter_text_clean_exit_on_immediate_close():
    """No messages, peer closes right away — handler should not raise."""
    app = Veloce()
    flags: list[str] = []

    @app.websocket("/x")
    async def h(ws: WebSocket):
        await ws.accept()
        async for _ in ws.iter_text():
            flags.append("got")
        flags.append("done")

    with TestClient(app) as client, client.websocket_connect("/x"):
        pass

    assert flags == ["done"]
