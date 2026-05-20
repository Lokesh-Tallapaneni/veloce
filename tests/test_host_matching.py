"""Route host= constraint — host-based route matching."""

from __future__ import annotations

from veloce import Veloce
from veloce.testclient import TestClient


def test_request_matching_host_is_served():
    app = Veloce()

    @app.get("/", host="api.example.com")
    async def api_root():
        return {"host": "api"}

    with TestClient(app) as client:
        resp = client.get("/", headers={"Host": "api.example.com"})
        assert resp.status_code == 200
        assert resp.json() == {"host": "api"}


def test_request_with_wrong_host_gets_404():
    app = Veloce()

    @app.get("/", host="api.example.com")
    async def api_root():
        return {}

    with TestClient(app) as client:
        resp = client.get("/", headers={"Host": "www.example.com"})
        assert resp.status_code == 404


def test_host_match_is_case_insensitive():
    app = Veloce()

    @app.get("/x", host="API.Example.COM")
    async def x():
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/x", headers={"Host": "api.example.com"})
        assert resp.status_code == 200


def test_host_match_ignores_port():
    app = Veloce()

    @app.get("/y", host="localhost")
    async def y():
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/y", headers={"Host": "localhost:8000"})
        assert resp.status_code == 200


def test_no_host_constraint_serves_any_host():
    app = Veloce()

    @app.get("/open")
    async def open_route():
        return {"open": True}

    with TestClient(app) as client:
        resp = client.get("/open", headers={"Host": "anything.test"})
        assert resp.status_code == 200
