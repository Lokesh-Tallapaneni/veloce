"""A status-code error handler is called the same way from every path.

`add_exception_handler(500, h)` registers one handler that three different code
paths can reach: an `HTTPException(500)` raised by a handler, an unhandled
exception shaped into a 500, and - for `405` - the method-not-allowed branch of
route resolution.

Only the first adapted to the handler's signature. The other two called it as
`handler(request=...)` outright, so a handler written `(request, exc)` - the
signature the documented decorator form uses, and the one the `HTTPException`
path passes - raised `TypeError`. That `TypeError` was raised *inside* the error
path, so it did not become a 500 either: it escaped dispatch entirely.

    app.add_exception_handler(500, h)   # async def h(request, exc)

    raise HTTPException(500)  -> h(request, exc)   200 OK
    raise RuntimeError()      -> h(request)        TypeError, uncaught
    POST to a GET-only route  -> h(request)        TypeError, uncaught

All three now go through the same signature adapter the `HTTPException` path
used. A one-argument handler keeps working; a two-argument one gets the real
exception where there is one, and an `HTTPException` carrying the status where
there is not.
"""

from __future__ import annotations

import pytest

from veloce import HTTPException, JSONResponse, Request, Veloce
from veloce.testclient import TestClient


def _app(handler_500=None, handler_405=None) -> tuple[Veloce, list]:
    app = Veloce(openapi_url=None)
    seen: list = []
    if handler_500 is not None:
        app.add_exception_handler(500, handler_500(seen))
    if handler_405 is not None:
        app.add_exception_handler(405, handler_405(seen))

    @app.get("/http")
    async def http(request: Request):
        raise HTTPException(500, "declared")

    @app.get("/crash")
    async def crash(request: Request):
        raise RuntimeError("undeclared")

    @app.get("/get-only")
    async def get_only(request: Request):
        return {}

    return app, seen


def _two_arg(seen):
    async def handler(request, exc):
        seen.append(type(exc).__name__)
        return JSONResponse({"via": "two-arg"}, status_code=599)

    return handler


def _one_arg(seen):
    async def handler(request):
        seen.append("one-arg")
        return JSONResponse({"via": "one-arg"}, status_code=599)

    return handler


# ── a two-argument handler works on every path ───────────────────────


def test_two_arg_handler_on_the_http_exception_path():
    """The path that already worked; it must keep working."""
    app, seen = _app(handler_500=_two_arg)
    with TestClient(app) as client:
        resp = client.get("/http")
    assert resp.json() == {"via": "two-arg"}
    assert seen == ["HTTPException"]


def test_two_arg_handler_on_the_unhandled_exception_path():
    """The defect: this used to raise `TypeError` out of dispatch."""
    app, seen = _app(handler_500=_two_arg)
    with TestClient(app) as client:
        resp = client.get("/crash")
    assert resp.json() == {"via": "two-arg"}


def test_the_unhandled_exception_path_passes_the_real_exception():
    """Not a stand-in - the handler sees what actually failed."""
    app, seen = _app(handler_500=_two_arg)
    with TestClient(app) as client:
        client.get("/crash")
    assert seen == ["RuntimeError"]


def test_two_arg_handler_on_the_method_not_allowed_path():
    app, seen = _app(handler_405=_two_arg)
    with TestClient(app) as client:
        resp = client.post("/get-only")
    assert resp.json() == {"via": "two-arg"}


def test_the_method_not_allowed_path_passes_an_http_exception():
    """No real exception exists there, so one carrying the status is supplied."""
    app, seen = _app(handler_405=_two_arg)
    with TestClient(app) as client:
        client.post("/get-only")
    assert seen == ["HTTPException"]


def test_the_supplied_exception_carries_the_status_code():
    captured: list = []

    def handler_factory(seen):
        async def handler(request, exc):
            captured.append(exc.status_code)
            return JSONResponse({}, status_code=405)

        return handler

    app, _ = _app(handler_405=handler_factory)
    with TestClient(app) as client:
        client.post("/get-only")
    assert captured == [405]


# ── a one-argument handler still works on every path ─────────────────
#
# The negative. A "fix" that always passed two arguments would break every
# handler that takes only the request.


@pytest.mark.parametrize(
    ("path", "method"),
    [("/http", "GET"), ("/crash", "GET")],
)
def test_one_arg_handler_still_works(path, method):
    app, seen = _app(handler_500=_one_arg)
    with TestClient(app) as client:
        resp = client.request(method, path)
    assert resp.json() == {"via": "one-arg"}


def test_one_arg_handler_on_the_method_not_allowed_path():
    app, seen = _app(handler_405=_one_arg)
    with TestClient(app) as client:
        assert client.post("/get-only").json() == {"via": "one-arg"}


# ── and with no handler registered, the defaults are unchanged ───────


def test_an_unhandled_exception_is_still_a_500():
    app, _ = _app()
    with TestClient(app) as client:
        resp = client.get("/crash")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Internal Server Error"


def test_a_wrong_method_is_still_a_405_with_allow():
    app, _ = _app()
    with TestClient(app) as client:
        resp = client.post("/get-only")
    assert resp.status_code == 405
    assert "GET" in resp.headers["allow"]


def test_a_declared_http_exception_is_still_shaped():
    app, _ = _app()
    with TestClient(app) as client:
        assert client.get("/http").status_code == 500
