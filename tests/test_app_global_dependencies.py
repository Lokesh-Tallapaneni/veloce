"""Veloce(dependencies=...) — app-level global dependencies."""

from __future__ import annotations

from veloce import Depends, HTTPException, Request, Veloce
from veloce.testclient import TestClient


def _require_token(request: Request) -> None:
    if request.headers.get("x-token") != "secret":
        raise HTTPException(status_code=401, detail="bad token")


def test_global_dependency_runs_for_every_route():
    app = Veloce(openapi_url=None, dependencies=[Depends(_require_token)])

    @app.get("/a")
    async def a(request: Request):
        return {"route": "a"}

    @app.get("/b")
    async def b(request: Request):
        return {"route": "b"}

    with TestClient(app) as client:
        assert client.get("/a").status_code == 401
        assert client.get("/b").status_code == 401
        assert client.get("/a", headers={"x-token": "secret"}).json() == {"route": "a"}
        assert client.get("/b", headers={"x-token": "secret"}).json() == {"route": "b"}


def test_no_global_dependencies_by_default():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(request: Request):
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/x").json() == {"ok": True}


def test_global_responses_overlaid_in_openapi():
    app = Veloce(responses={404: {"description": "Missing"}})

    @app.get("/x")
    async def x(request: Request):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    responses = schema["paths"]["/x"]["get"]["responses"]
    assert "404" in responses
    assert responses["404"]["description"] == "Missing"


def test_per_route_dependency_runs_after_global():
    app = Veloce(openapi_url=None, dependencies=[Depends(_require_token)])
    order: list[str] = []

    def _route_dep() -> None:
        order.append("route")

    @app.get("/x", dependencies=[Depends(_route_dep)])
    async def x(request: Request):
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/x", headers={"x-token": "secret"})

    assert resp.status_code == 200
    assert order == ["route"]
