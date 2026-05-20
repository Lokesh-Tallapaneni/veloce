"""Veloce.handle_http_exception / handle_user_exception / log_exception."""

from __future__ import annotations

import logging

import pytest

from veloce import HTTPException, JSONResponse, Veloce
from veloce.exceptions import Forbidden, NotFound


@pytest.mark.asyncio
async def test_handle_http_exception_default_body():
    app = Veloce(debug=True, openapi_url=None)
    resp = await app.handle_http_exception(NotFound("missing"))
    assert resp.status_code == 404
    import orjson

    assert orjson.loads(resp.body) == {"detail": "missing"}


@pytest.mark.asyncio
async def test_handle_http_exception_uses_status_handler():
    app = Veloce(debug=True, openapi_url=None)

    @app.errorhandler(404)
    async def custom(request, exc):
        return JSONResponse({"custom": True}, status_code=404)

    resp = await app.handle_http_exception(NotFound())
    import orjson

    assert orjson.loads(resp.body) == {"custom": True}


@pytest.mark.asyncio
async def test_handle_http_exception_passes_headers():
    app = Veloce(debug=True, openapi_url=None)
    exc = Forbidden("nope")
    exc.headers = {"X-Reason": "private"}
    resp = await app.handle_http_exception(exc)
    assert resp.headers.get("X-Reason") == "private"


@pytest.mark.asyncio
async def test_handle_user_exception_http_routes_through_http_path():
    app = Veloce(debug=True, openapi_url=None)
    resp = await app.handle_user_exception(NotFound("nope"))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_handle_user_exception_arbitrary_with_handler():
    app = Veloce(debug=True, openapi_url=None)

    class MyError(Exception):
        pass

    @app.errorhandler(MyError)
    async def handler(request, exc):
        return {"err": str(exc)}

    resp = await app.handle_user_exception(MyError("boom"))
    import orjson

    assert orjson.loads(resp.body) == {"err": "boom"}


@pytest.mark.asyncio
async def test_handle_user_exception_unhandled_returns_500(caplog):
    app = Veloce(debug=True, openapi_url=None)
    caplog.set_level(logging.ERROR, logger=app.logger.name)

    class Random(Exception):
        pass

    resp = await app.handle_user_exception(Random("kaboom"))
    assert resp.status_code == 500
    # `log_exception` ran.
    assert any("Exception on request" in r.message for r in caplog.records)


def test_log_exception_calls_logger(caplog):
    app = Veloce(openapi_url=None)
    caplog.set_level(logging.ERROR, logger=app.logger.name)
    try:
        raise RuntimeError("test")
    except RuntimeError as e:
        app.log_exception(e)
    assert any("Exception on request" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_handle_http_exception_bare():
    """Untyped HTTPException (no detail/headers) gets sensible defaults."""
    app = Veloce(debug=True, openapi_url=None)
    resp = await app.handle_http_exception(HTTPException(418, "i am a teapot"))
    assert resp.status_code == 418
    import orjson

    assert orjson.loads(resp.body) == {"detail": "i am a teapot"}
