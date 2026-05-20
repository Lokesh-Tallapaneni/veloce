"""app.add_api_route imperative route registration."""

from __future__ import annotations

from veloce import Veloce
from veloce.contrib.openapi import get_openapi_schema
from veloce.testclient import TestClient


def test_add_api_route_default_get():
    app = Veloce()

    async def handler():
        return {"ok": True}

    app.add_api_route("/x", handler)
    with TestClient(app) as client:
        resp = client.get("/x")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


def test_add_api_route_explicit_methods():
    app = Veloce()

    async def create():
        return {"created": True}

    app.add_api_route("/items", create, methods=["POST"])
    with TestClient(app) as client:
        assert client.post("/items").json() == {"created": True}


def test_add_api_route_forwards_route_kwargs():
    app = Veloce()

    async def handler():
        return {}

    app.add_api_route("/tagged", handler, methods=["GET"], tags=["admin"], summary="Tagged route")
    schema = get_openapi_schema(app)
    op = schema["paths"]["/tagged"]["get"]
    assert op["tags"] == ["admin"]
    assert op["summary"] == "Tagged route"


def test_add_api_route_status_code():
    app = Veloce()

    async def handler():
        return {"made": True}

    app.add_api_route("/new", handler, methods=["POST"], status_code=201)
    with TestClient(app) as client:
        resp = client.post("/new")
        assert resp.status_code == 201


def test_add_api_route_openapi_extra_passthrough():
    app = Veloce()

    async def handler():
        return {}

    app.add_api_route("/ext", handler, methods=["GET"], openapi_extra={"x-internal": True})
    schema = get_openapi_schema(app)
    assert schema["paths"]["/ext"]["get"]["x-internal"] is True
