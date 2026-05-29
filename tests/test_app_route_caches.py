"""Route-introspection caches on Veloce: routes, view_functions, url_map.

The properties return the same cached object on repeated access and a
fresh one only after the route table mutates.
"""

from __future__ import annotations

import inspect
import logging

from veloce import Veloce
from veloce.app import _URLMap


def _make_app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {}

    return app


def test_routes_cached_until_route_added():
    app = _make_app()
    first = app.routes
    second = app.routes
    assert first is second

    @app.get("/another")
    async def another():
        return {}

    third = app.routes
    assert third is not first
    assert len(third) == len(first) + 1


def test_view_functions_cached_until_route_added():
    """`view_functions` returns a fresh snapshot per call (so mutation
    cannot poison framework state), but the underlying route-walk is
    cached until a new route is registered.
    """
    app = _make_app()
    first = app.view_functions
    second = app.view_functions
    assert first == second
    assert "index" in first

    @app.get("/another")
    async def another():
        return {}

    third = app.view_functions
    assert "another" in third
    assert third != first


def test_url_map_cached_until_route_added():
    app = _make_app()
    first = app.url_map
    second = app.url_map
    assert first is second
    assert isinstance(first, _URLMap)

    # _URLMap's internal _build() cache also stable across access
    list_a = list(first)
    list_b = list(first)
    assert [r.endpoint for r in list_a] == [r.endpoint for r in list_b]

    @app.get("/another")
    async def another():
        return {}

    third = app.url_map
    assert third is not first
    assert len(list(third)) == len(list_a) + 1


def test_caches_invalidated_by_register_blueprint():
    from veloce.blueprints import Blueprint

    app = _make_app()
    routes_before = app.routes
    bp = Blueprint("api", url_prefix="/api")

    @bp.get("/widgets")
    async def widgets():
        return {}

    app.register_blueprint(bp)
    routes_after = app.routes
    assert routes_after is not routes_before
    assert any(r["name"] == "api.widgets" for r in routes_after)


def test_caches_invalidated_by_include_router():
    from veloce.routing.router import Router

    app = _make_app()
    routes_before = app.routes
    sub = Router()

    @sub.get("/sub")
    async def sub_handler():
        return {}

    app.include_router(sub, prefix="/v1")
    routes_after = app.routes
    assert routes_after is not routes_before
    assert any(r["path"] == "/v1/sub" for r in routes_after)


# ── run() default host ──────────────────────────────────────────


def test_run_host_default_is_loopback():
    # Don't actually start uvicorn — inspect the signature default.
    # `host` is a None sentinel; resolution happens inside run() and
    # yields "127.0.0.1" when neither host nor bind_all is provided.
    sig = inspect.signature(Veloce.run)
    assert sig.parameters["host"].default is None
    assert sig.parameters["bind_all"].default is False

    import unittest.mock

    app = _make_app()
    with unittest.mock.patch.object(Veloce, "_serve") as mock_serve:
        mock_serve.side_effect = KeyboardInterrupt
        app.run(access_log=False)
    resolved_host = mock_serve.call_args.args[0]
    assert resolved_host == "127.0.0.1"


def test_run_bind_all_parameter_present():
    sig = inspect.signature(Veloce.run)
    assert "bind_all" in sig.parameters


def test_bind_all_with_explicit_host_raises_value_error():
    import pytest

    app = _make_app()
    with pytest.raises(ValueError, match="bind_all"):
        app.run(host="192.168.1.10", bind_all=True)


# ── background task error logging ───────────────────────────────


def test_background_task_failure_is_logged(caplog):
    import asyncio

    from veloce.background import BackgroundTask

    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        async def boom():
            raise RuntimeError("kaboom")

        from veloce.http.response import Response

        return Response(body=b"ok", background=BackgroundTask(boom))

    from veloce.testclient import TestClient

    with caplog.at_level(logging.ERROR, logger=app.logger.name), TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200

        # Drain the loop so the background task's done-callback fires.
        async def _drain():
            for _ in range(5):
                await asyncio.sleep(0)

        client._loop.run_until_complete(_drain())

    matches = [r for r in caplog.records if "Background task failed" in r.getMessage()]
    assert matches, "expected background-task failure to be logged"
    assert matches[0].levelno == logging.ERROR


# ── _coerce_response Pydantic detection ─────────────────────────


def test_coerce_response_does_not_treat_duck_model_dump_as_pydantic():
    """A plain object with a `.model_dump` attribute must NOT be coerced
    as if it were a Pydantic model.

    Before the fix, `hasattr(result, "model_dump")` returned True for any
    object happening to define that name; the result was a JSONResponse
    built from `result.model_dump()`, silently masking real bugs.
    """

    app = Veloce(openapi_url=None)

    class DuckTyped:
        # Not a Pydantic BaseModel — just has a method called model_dump.
        def model_dump(self):  # pragma: no cover - must not be called
            raise AssertionError("should not be treated as Pydantic")

    import pytest

    obj = DuckTyped()
    # The Pydantic branch must NOT be taken — `.model_dump` (which would
    # raise AssertionError) must not be called. Instead the object falls
    # through to the final `JSONResponse(result)` branch and the JSON
    # encoder raises a serialization error.
    with pytest.raises(ValueError, match="not JSON-serializable"):
        app._coerce_response(obj)


def test_coerce_response_handles_real_pydantic_model():
    from pydantic import BaseModel

    from veloce.http.response import JSONResponse

    class Item(BaseModel):
        name: str

    app = Veloce(openapi_url=None)
    response = app._coerce_response(Item(name="foo"))
    assert isinstance(response, JSONResponse)
