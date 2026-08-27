"""@app.errorhandler alias for exception_handler."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import HTTPException, Request, Veloce, abort


def _req(path: str = "/x") -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


async def test_errorhandler_status_code():
    app = Veloce(debug=True, openapi_url=None)

    @app.errorhandler(404)
    async def not_found(request, exc):
        return {"oops": "missing", "path": request.path}

    resp = await app.handle_request(_req("/nope"))
    assert resp.status_code == 200  # the handler returned 200 by default
    import orjson

    body = orjson.loads(resp.body)
    assert body == {"oops": "missing", "path": "/nope"}


async def test_errorhandler_exception_class():
    app = Veloce(debug=True, openapi_url=None)

    class MyError(Exception):
        pass

    @app.errorhandler(MyError)
    async def handle(request, exc):
        return {"err": str(exc)}

    @app.get("/x")
    async def x():
        raise MyError("boom")

    resp = await app.handle_request(_req("/x"))
    import orjson

    assert orjson.loads(resp.body) == {"err": "boom"}


def test_errorhandler_is_same_object_as_exception_handler():
    """The alias points at the same callable — no semantic drift."""
    app = Veloce(openapi_url=None)
    assert app.errorhandler is app.exception_handler or (
        # If bound-method comparison fails on some Python versions, the
        # underlying function should be identical.
        getattr(app.errorhandler, "__func__", None)
        is getattr(app.exception_handler, "__func__", None)
    )


async def test_errorhandler_for_httpexception_base():
    """Registering against the base HTTPException catches subclasses via MRO."""
    app = Veloce(debug=True, openapi_url=None)

    @app.errorhandler(HTTPException)
    async def handle_http(request, exc):
        return {"caught": exc.status_code}

    @app.get("/x")
    async def x():

        abort(403)

    resp = await app.handle_request(_req("/x"))
    import orjson

    assert orjson.loads(resp.body) == {"caught": 403}
