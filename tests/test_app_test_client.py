"""Veloce.test_client() factory."""

from __future__ import annotations

from veloce import Veloce
from veloce.testclient import TestClient


def test_returns_testclient_bound_to_app():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/hello")
    async def hello():
        return {"ok": True}

    client = app.test_client()
    assert isinstance(client, TestClient)
    resp = client.get("/hello")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_kwargs_forwarded():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/from")
    async def from_(request):
        from veloce import redirect

        return redirect("/to", code=302)

    @app.get("/to")
    async def to():
        return {"landed": True}

    client = app.test_client(follow_redirects=True)
    resp = client.get("/from")
    assert resp.status_code == 200
    assert resp.json() == {"landed": True}
