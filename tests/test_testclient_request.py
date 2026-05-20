"""TestClient.request() — generic verb-agnostic dispatcher."""

from __future__ import annotations

from veloce import Request, Veloce
from veloce.testclient import TestClient


def _app() -> Veloce:
    app = Veloce()

    @app.get("/g")
    async def g():
        return {"verb": "GET"}

    @app.post("/p")
    async def p(request: Request):
        return {"verb": "POST", "body": request.json()}

    @app.patch("/pa")
    async def pa():
        return {"verb": "PATCH"}

    @app.delete("/d")
    async def d():
        return {"verb": "DELETE"}

    return app


def test_request_get():
    with TestClient(_app()) as client:
        resp = client.request("GET", "/g")
        assert resp.status_code == 200
        assert resp.json() == {"verb": "GET"}


def test_request_post_with_json():
    with TestClient(_app()) as client:
        resp = client.request("POST", "/p", json={"k": "v"})
        assert resp.json() == {"verb": "POST", "body": {"k": "v"}}


def test_request_patch():
    with TestClient(_app()) as client:
        assert client.request("PATCH", "/pa").json() == {"verb": "PATCH"}


def test_request_delete():
    with TestClient(_app()) as client:
        assert client.request("DELETE", "/d").json() == {"verb": "DELETE"}


def test_request_lowercase_method():
    with TestClient(_app()) as client:
        # Method is upper-cased internally.
        assert client.request("get", "/g").status_code == 200


def test_request_with_params():
    app = Veloce()

    @app.get("/q")
    async def q(request: Request):
        return {"got": request.query_params.get("x")}

    with TestClient(app) as client:
        resp = client.request("GET", "/q", params={"x": "42"})
        assert resp.json() == {"got": "42"}
