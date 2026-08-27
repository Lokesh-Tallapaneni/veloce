"""app.add_exception_handler — imperative exception-handler registration."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import HTMLResponse, JSONResponse, Request, Veloce
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


def test_a_class_key_handles_that_exception():
    """Registration used to be asserted by reading `_exception_handlers`. What
    registration is *for* is that raising the exception reaches the handler,
    which is both the public behaviour and the stronger claim."""
    app = Veloce(openapi_url=None)

    async def h(request, exc):
        return JSONResponse({"handled": "class"}, status_code=418)

    app.add_exception_handler(CustomError, h)

    @app.get("/boom")
    async def boom(request: Request):
        raise CustomError("x")

    with TestClient(app) as client:
        resp = client.get("/boom")
    assert resp.status_code == 418
    assert resp.json() == {"handled": "class"}


def test_a_status_key_handles_that_status():
    """The other table, distinguished by an `int` key rather than a class."""
    app = Veloce(openapi_url=None)

    async def h(request, exc):
        return JSONResponse({"handled": "status"}, status_code=500)

    app.add_exception_handler(500, h)

    @app.get("/crash")
    async def crash(request: Request):
        raise RuntimeError("x")

    with TestClient(app) as client:
        resp = client.get("/crash")
    assert resp.status_code == 500
    assert resp.json() == {"handled": "status"}


def test_a_class_key_does_not_answer_an_unrelated_exception():
    """The negative: a table that caught everything would pass the two above."""
    app = Veloce(openapi_url=None)

    async def h(request, exc):
        return JSONResponse({"handled": "class"}, status_code=418)

    app.add_exception_handler(CustomError, h)

    @app.get("/other")
    async def other(request: Request):
        raise ValueError("x")

    with TestClient(app) as client:
        assert client.get("/other").status_code != 418


def test_decorator_and_imperative_equivalent():
    """Both forms produce the same behaviour, asserted by running it - the old
    version compared table membership, which two different handlers would also
    satisfy."""
    responses = []
    for app in (Veloce(openapi_url=None), Veloce(openapi_url=None)):
        responses.append(app)

    app_dec, app_imp = responses

    @app_dec.exception_handler(CustomError)
    async def _h(request, exc):
        return JSONResponse({"via": "handler"}, status_code=418)

    async def h(request, exc):
        return JSONResponse({"via": "handler"}, status_code=418)

    app_imp.add_exception_handler(CustomError, h)

    for app in (app_dec, app_imp):

        @app.get("/boom")
        async def boom(request: Request):
            raise CustomError("x")

    with TestClient(app_dec) as dec, TestClient(app_imp) as imp:
        first, second = dec.get("/boom"), imp.get("/boom")
    assert first.status_code == second.status_code == 418
    assert first.json() == second.json() == {"via": "handler"}


async def test_register_by_status_code():
    app = Veloce(openapi_url=None)

    async def handle_404(request, exc):
        return JSONResponse({"custom": "not found"}, status_code=404)

    app.register_error_handler(404, handle_404)

    resp = await app.handle_request(make_request(path="/nonexistent"))
    assert resp.status_code == 404
    assert b"custom" in resp.body


async def test_register_by_exception_class():
    app = Veloce(openapi_url=None)

    class CustomError(Exception):
        pass

    async def handle_custom(request, exc):
        return JSONResponse({"error": "custom"}, status_code=500)

    app.register_error_handler(CustomError, handle_custom)

    @app.get("/fail")
    async def fail(request: Request):
        raise CustomError("boom")

    resp = await app.handle_request(make_request(path="/fail"))
    assert b"custom" in resp.body


async def test_custom_404_handler():
    app = Veloce(openapi_url=None)

    @app.exception_handler(404)
    async def custom_404(request: Request):
        return HTMLResponse("<h1>Custom 404</h1>", status_code=404)

    resp = await app.handle_request(make_request(path="/nonexistent"))
    assert resp.status_code == 404
    assert b"Custom 404" in resp.body


async def test_custom_500_handler():
    app = Veloce(openapi_url=None)

    @app.exception_handler(500)
    async def custom_500(request: Request):
        return JSONResponse({"error": "custom 500"}, status_code=500)

    @app.get("/crash")
    async def crash(request: Request):
        raise RuntimeError("boom")

    resp = await app.handle_request(make_request(path="/crash"))
    assert resp.status_code == 500
    assert b"custom 500" in resp.body


def test_exception_handler_decorator_invalidates_mro_cache():
    """A handler decorated AFTER a prior raise populates the negative
    cache must still take effect for the cached subclass type. Before
    the fix, the late registration was silently shadowed by the stale
    `_exc_handler_cache[ValueError] = None` entry."""
    app = Veloce(openapi_url=None)

    @app.get("/raise")
    def raiser():
        raise ValueError("boom")

    client = TestClient(app)
    # First request populates the cache with `None` for ValueError →
    # the dispatcher falls back to the framework 500.
    resp_no_handler = client.get("/raise")
    assert resp_no_handler.status_code == 500

    # Register a handler AFTER the cache was populated.
    @app.exception_handler(ValueError)
    def handle_value_error(request, exc):
        from veloce.http.response import JSONResponse

        return JSONResponse({"caught": str(exc)}, status_code=418)

    # The decorator must clear the cache so the new handler fires.
    resp_with_handler = client.get("/raise")
    assert resp_with_handler.status_code == 418
    assert resp_with_handler.json() == {"caught": "boom"}


def test_add_exception_handler_imperative_invalidates_mro_cache():
    """Same invariant via the imperative `add_exception_handler` shape."""
    app = Veloce(openapi_url=None)

    @app.get("/raise")
    def raiser():
        raise KeyError("missing")

    client = TestClient(app)
    assert client.get("/raise").status_code == 500

    def handle_key_error(request, exc):
        from veloce.http.response import JSONResponse

        return JSONResponse({"key": str(exc)}, status_code=422)

    app.add_exception_handler(KeyError, handle_key_error)

    resp = client.get("/raise")
    assert resp.status_code == 422
