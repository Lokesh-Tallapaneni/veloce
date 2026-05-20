"""app.add_exception_handler — imperative exception-handler registration."""

from __future__ import annotations

from veloce import JSONResponse, Veloce
from veloce.testclient import TestClient


class CustomError(Exception):
    pass


def test_add_exception_handler_by_class():
    app = Veloce()

    async def handle(request, exc):
        return JSONResponse({"caught": str(exc)}, status_code=418)

    app.add_exception_handler(CustomError, handle)

    @app.get("/boom")
    async def boom():
        raise CustomError("kaboom")

    with TestClient(app) as client:
        resp = client.get("/boom")
        assert resp.status_code == 418
        assert resp.json() == {"caught": "kaboom"}


def test_add_exception_handler_by_status_code():
    app = Veloce()

    async def handle_404(request, exc):
        return JSONResponse({"msg": "nowhere"}, status_code=404)

    app.add_exception_handler(404, handle_404)

    with TestClient(app) as client:
        resp = client.get("/missing")
        assert resp.status_code == 404
        assert resp.json() == {"msg": "nowhere"}


def test_add_exception_handler_registers_in_class_table():
    app = Veloce()

    async def h(request, exc):
        return JSONResponse({})

    app.add_exception_handler(CustomError, h)
    assert app._exception_handlers[CustomError] is h


def test_add_exception_handler_registers_in_status_table():
    app = Veloce()

    async def h(request, exc):
        return JSONResponse({})

    app.add_exception_handler(500, h)
    assert app._status_handlers[500] is h


def test_decorator_and_imperative_equivalent():
    app_dec = Veloce()
    app_imp = Veloce()

    async def h(request, exc):
        return JSONResponse({})

    @app_dec.exception_handler(CustomError)
    async def _h(request, exc):
        return JSONResponse({})

    app_imp.add_exception_handler(CustomError, h)
    # Both land in the same table under the same key.
    assert CustomError in app_dec._exception_handlers
    assert CustomError in app_imp._exception_handlers
