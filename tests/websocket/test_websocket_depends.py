"""W8 — Depends() resolution on WebSocket handlers."""

from __future__ import annotations

from veloce import Depends, Veloce


def test_simple_depends_resolved():
    app = Veloce(debug=True, openapi_url=None)

    async def get_user():
        return {"name": "alice"}

    @app.websocket("/ws")
    async def echo(ws, user=Depends(get_user)):
        await ws.accept()
        await ws.send_text(user["name"])
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "alice"


def test_nested_depends():
    app = Veloce(debug=True, openapi_url=None)

    async def get_db():
        return {"conn": True}

    async def get_user(db=Depends(get_db)):
        return {"db_ok": db["conn"], "name": "bob"}

    @app.websocket("/ws")
    async def echo(ws, user=Depends(get_user)):
        await ws.accept()
        await ws.send_text(f"{user['name']}-{user['db_ok']}")
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "bob-True"


def test_dependency_cached_within_one_handshake():
    """Same dep referenced twice in one handler runs once."""
    app = Veloce(debug=True, openapi_url=None)
    calls: list[int] = []

    async def counter():
        calls.append(1)
        return len(calls)

    @app.websocket("/ws")
    async def echo(ws, a=Depends(counter), b=Depends(counter)):
        await ws.accept()
        await ws.send_text(f"{a}-{b}")
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "1-1"
    # Counter ran exactly once for the whole chain.
    assert len(calls) == 1


def test_sync_dependency_supported():
    """Plain sync `def` deps still work."""
    app = Veloce(debug=True, openapi_url=None)

    def get_config():
        return {"env": "test"}

    @app.websocket("/ws")
    async def echo(ws, cfg=Depends(get_config)):
        await ws.accept()
        await ws.send_text(cfg["env"])
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_text() == "test"
