"""Veloce(exception_handlers=...) — register handlers at construction."""

from __future__ import annotations

from veloce import HTTPException, Request, Veloce
from veloce.http.response import JSONResponse
from veloce.testclient import TestClient


class CustomError(Exception):
    pass


async def _handle_custom(request: Request, exc: CustomError) -> JSONResponse:
    return JSONResponse({"handled": "custom"}, status_code=418)


async def _handle_404(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"handled": "not-found"}, status_code=404)


def test_class_keyed_handler_registered_from_ctor():
    app = Veloce(openapi_url=None, exception_handlers={CustomError: _handle_custom})

    @app.get("/boom")
    async def boom(request: Request):
        raise CustomError("kaboom")

    with TestClient(app) as client:
        resp = client.get("/boom")

    assert resp.status_code == 418
    assert resp.json() == {"handled": "custom"}


def test_status_keyed_handler_registered_from_ctor():
    app = Veloce(openapi_url=None, exception_handlers={404: _handle_404})

    with TestClient(app) as client:
        resp = client.get("/does-not-exist")

    assert resp.status_code == 404
    assert resp.json() == {"handled": "not-found"}


def test_no_exception_handlers_by_default():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(request: Request):
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/x").json() == {"ok": True}


def test_multiple_handlers_from_ctor():
    app = Veloce(
        openapi_url=None,
        exception_handlers={CustomError: _handle_custom, 404: _handle_404},
    )

    @app.get("/boom")
    async def boom(request: Request):
        raise CustomError("x")

    with TestClient(app) as client:
        assert client.get("/boom").status_code == 418
        assert client.get("/missing").status_code == 404
