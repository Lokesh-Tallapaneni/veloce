"""PROPAGATE_EXCEPTIONS config flag (E9)."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import JSONResponse, Request, Veloce
from veloce.exceptions import NotFound


def _req(path: str = "/") -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


async def test_default_catches_exception_and_returns_500():
    """No config flag set → exceptions are caught, 500 returned."""
    app = Veloce(openapi_url=None)  # not debug, not testing

    @app.get("/boom")
    async def boom():
        raise RuntimeError("synthetic")

    resp = await app.handle_request(_req("/boom"))
    assert resp.status_code == 500


async def test_propagate_exceptions_true_reraises():
    """`PROPAGATE_EXCEPTIONS=True` → handler exception escapes dispatch."""
    app = Veloce(openapi_url=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True

    @app.get("/boom")
    async def boom():
        raise RuntimeError("synthetic")

    with pytest.raises(RuntimeError, match="synthetic"):
        await app.handle_request(_req("/boom"))


async def test_propagate_exceptions_false_overrides_implicit():
    """Explicit `PROPAGATE_EXCEPTIONS=False` wins even when DEBUG+TESTING
    are set. Lets users opt out of propagation in test+debug mode."""
    app = Veloce(openapi_url=None)
    app.config["DEBUG"] = True
    app.config["TESTING"] = True
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.get("/boom")
    async def boom():
        raise RuntimeError("synthetic")

    # Caught, returned as a (debug-mode plaintext) 500.
    resp = await app.handle_request(_req("/boom"))
    assert resp.status_code == 500


async def test_propagate_implicit_when_debug_and_testing():
    """Both DEBUG and TESTING set → implicit propagation, no explicit flag needed."""
    app = Veloce(openapi_url=None)
    app.config["DEBUG"] = True
    app.config["TESTING"] = True

    @app.get("/boom")
    async def boom():
        raise RuntimeError("synthetic")

    with pytest.raises(RuntimeError):
        await app.handle_request(_req("/boom"))


async def test_debug_alone_does_not_propagate():
    """Just DEBUG without TESTING is NOT propagating — Veloce still
    catches and returns a 500 (the existing debug-traceback rendering uses
    the ctor `debug=True`, not `config['DEBUG']`; PROPAGATE_EXCEPTIONS is
    the only flag that controls re-raising)."""
    app = Veloce(openapi_url=None)
    app.config["DEBUG"] = True
    # TESTING explicitly False — no implicit propagation.

    @app.get("/boom")
    async def boom():
        raise RuntimeError("synthetic")

    resp = await app.handle_request(_req("/boom"))
    assert resp.status_code == 500


async def test_registered_handler_still_wins_over_propagate():
    """If a user has registered a handler for the exception type, that
    handler runs even when PROPAGATE_EXCEPTIONS=True — propagation only
    happens after the handler lookup misses."""
    app = Veloce(openapi_url=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True

    @app.exception_handler(RuntimeError)
    async def on_runtime(request, exc):

        return JSONResponse({"caught": True}, status_code=599)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("synthetic")

    resp = await app.handle_request(_req("/boom"))
    assert resp.status_code == 599


async def test_propagate_exceptions_env_string_false_is_off():
    """An env-file loader stores `PROPAGATE_EXCEPTIONS=false` as the string
    `"false"`; it must read as off, not as a truthy non-empty string."""
    app = Veloce(openapi_url=None)
    app.config["PROPAGATE_EXCEPTIONS"] = "false"

    @app.get("/boom")
    async def boom():
        raise RuntimeError("synthetic")

    resp = await app.handle_request(_req("/boom"))
    assert resp.status_code == 500


async def test_propagate_exceptions_env_string_true_reraises():
    """The string `"true"` from an env source enables propagation."""
    app = Veloce(openapi_url=None)
    app.config["PROPAGATE_EXCEPTIONS"] = "true"

    @app.get("/boom")
    async def boom():
        raise RuntimeError("synthetic")

    with pytest.raises(RuntimeError, match="synthetic"):
        await app.handle_request(_req("/boom"))


async def test_http_exception_not_propagated():
    """The PROPAGATE flag only affects the generic `except Exception`
    branch. HTTPExceptions are framework-managed and always produce a
    structured JSON response."""

    app = Veloce(openapi_url=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True

    @app.get("/x")
    async def x():
        raise NotFound("missing")

    resp = await app.handle_request(_req("/x"))
    assert resp.status_code == 404
