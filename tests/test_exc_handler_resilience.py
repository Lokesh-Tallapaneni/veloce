"""Resilience of the dispatch error path when a user exception handler raises.

A user-registered exception handler that itself raises must not escape
dispatch uncaught. In production (no `PROPAGATE_EXCEPTIONS`) the secondary
failure is logged - naming the handler and the request path - and a standard
500 is returned. When `PROPAGATE_EXCEPTIONS` is in effect, the handler bug is
re-raised so tests and dev surface it.
"""

from __future__ import annotations

import logging

from veloce import HTTPException, JSONResponse, Request, TestClient, Veloce


def _app_with_buggy_handler(*, on_http: bool, propagate: bool) -> Veloce:
    app = Veloce(openapi_url=None)
    if propagate:
        app.config["PROPAGATE_EXCEPTIONS"] = True

    if on_http:

        @app.exception_handler(HTTPException)
        async def handle_http(request: Request, exc: HTTPException):
            raise RuntimeError("handler is buggy")

        @app.get("/boom")
        async def boom_http():
            raise HTTPException(400, "bad request")

    else:

        @app.exception_handler(ValueError)
        async def handle_value(request: Request, exc: ValueError):
            raise RuntimeError("handler is buggy")

        @app.get("/boom")
        async def boom_value():
            raise ValueError("original failure")

    return app


def test_buggy_http_handler_returns_500_not_uncaught(caplog):
    app = _app_with_buggy_handler(on_http=True, propagate=False)
    with TestClient(app) as client, caplog.at_level(logging.ERROR):
        resp = client.get("/boom")
    assert resp.status_code == 500
    body = resp.json()
    assert body["status_code"] == 500
    # The secondary failure is logged with the handler name and path.
    assert any("handle_http" in r.getMessage() for r in caplog.records)
    assert any("/boom" in r.getMessage() for r in caplog.records)


def test_buggy_generic_handler_returns_500_not_uncaught(caplog):
    app = _app_with_buggy_handler(on_http=False, propagate=False)
    with TestClient(app) as client, caplog.at_level(logging.ERROR):
        resp = client.get("/boom")
    assert resp.status_code == 500
    assert resp.json()["status_code"] == 500
    assert any("handle_value" in r.getMessage() for r in caplog.records)


def test_buggy_handler_reraises_under_propagate():
    import pytest

    app = _app_with_buggy_handler(on_http=True, propagate=True)
    with TestClient(app) as client, pytest.raises(RuntimeError, match="handler is buggy"):
        client.get("/boom")


def test_well_behaved_handler_is_unaffected():
    app = Veloce(openapi_url=None)

    @app.exception_handler(ValueError)
    async def handle_value(request: Request, exc: ValueError):
        return JSONResponse({"handled": str(exc)}, status_code=422)

    @app.get("/boom")
    async def boom():
        raise ValueError("nope")

    with TestClient(app) as client:
        resp = client.get("/boom")
    assert resp.status_code == 422
    assert resp.json() == {"handled": "nope"}


def test_original_exception_kept_as_context():
    """The original exception is chained onto the secondary one for logs."""
    captured = {}
    app = Veloce(openapi_url=None)

    @app.exception_handler(ValueError)
    async def handle_value(request: Request, exc: ValueError):
        raise RuntimeError("secondary")

    @app.get("/boom")
    async def boom():
        raise ValueError("primary")

    class _Capture(logging.Handler):
        def emit(self, record):
            if record.exc_info:
                captured["exc"] = record.exc_info[1]

    handler = _Capture()
    app.logger.addHandler(handler)
    try:
        with TestClient(app) as client:
            client.get("/boom")
    finally:
        app.logger.removeHandler(handler)

    assert isinstance(captured.get("exc"), RuntimeError)
    assert isinstance(captured["exc"].__context__, ValueError)
