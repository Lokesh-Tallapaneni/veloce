"""Veloce(middleware=[...]) — register middleware at construction."""

from __future__ import annotations

from veloce import Middleware, Request, Veloce
from veloce.http.response import Response
from veloce.testclient import TestClient


class StampMiddleware(Middleware):
    """Adds a marker header to every response."""

    def __init__(self, value: str = "yes") -> None:
        self.value = value

    async def process_response(self, request: Request, response: Response) -> Response:
        response.headers["X-Stamped"] = self.value
        return response


def test_middleware_from_ctor_runs():
    app = Veloce(openapi_url=None, middleware=[StampMiddleware()])

    @app.get("/x")
    async def x(request: Request):
        return {"ok": True}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.headers.get("x-stamped") == "yes"


def test_middleware_ctor_value_passed_through():
    app = Veloce(openapi_url=None, middleware=[StampMiddleware(value="custom")])

    @app.get("/x")
    async def x(request: Request):
        return {}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.headers.get("x-stamped") == "custom"


def test_no_middleware_by_default():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(request: Request):
        return {}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert "x-stamped" not in {k.lower() for k in resp.headers}


def test_multiple_middleware_from_ctor():
    class SecondMiddleware(Middleware):
        async def process_response(self, request: Request, response: Response) -> Response:
            response.headers["X-Second"] = "1"
            return response

    app = Veloce(
        openapi_url=None,
        middleware=[StampMiddleware(), SecondMiddleware()],
    )

    @app.get("/x")
    async def x(request: Request):
        return {}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.headers.get("x-stamped") == "yes"
    assert resp.headers.get("x-second") == "1"
