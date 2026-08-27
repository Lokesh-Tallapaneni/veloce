"""Route-introspection caches on Veloce: routes, view_functions, url_map.

The properties return the same cached object on repeated access and a
fresh one only after the route table mutates.
"""

from __future__ import annotations

import inspect
import logging

import pytest

from veloce import Veloce
from veloce.app import _URLMap
from veloce.background import BackgroundTask
from veloce.blueprints import Blueprint
from veloce.http.response import JSONResponse, Response
from veloce.routing.router import Router
from veloce.testclient import TestClient


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
    app = _make_app()
    with pytest.raises(ValueError, match="bind_all"):
        app.run(host="192.168.1.10", bind_all=True)


# ── background task error logging ───────────────────────────────


def test_background_task_failure_is_logged(caplog):

    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        async def boom():
            raise RuntimeError("kaboom")

        return Response(body=b"ok", background=BackgroundTask(boom))

    with caplog.at_level(logging.ERROR, logger=app.logger.name), TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200

        # Wait for the task itself rather than guessing at loop turns; the
        # done-callback that logs the failure has fired once this returns.
        assert client.wait_for_background_tasks() is True

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

    obj = DuckTyped()
    # The Pydantic branch must NOT be taken — `.model_dump` (which would
    # raise AssertionError) must not be called. Instead the object falls
    # through to the final `JSONResponse(result)` branch. The JSON encoder's
    # `default=` fallback serialises the plain object via `vars()` (no public
    # attributes here, so an empty object), confirming the Pydantic path was
    # skipped — had it been taken, the AssertionError above would surface.
    resp = app._coerce_response(obj)
    assert isinstance(resp, JSONResponse)
    assert resp.body == b"{}"


def test_coerce_response_handles_real_pydantic_model():
    from pydantic import BaseModel

    class Item(BaseModel):
        name: str

    app = Veloce(openapi_url=None)
    response = app._coerce_response(Item(name="foo"))
    assert isinstance(response, JSONResponse)


# Moved here from `test_app_protocol_signals_e2e.py`, a module named for a fix
# batch rather than a subject. The duck-typing test below is the end-to-end
# counterpart of `test_coerce_response_does_not_treat_duck_model_dump_as_pydantic`
# above: that one calls `_coerce_response` directly and proves `.model_dump()` is
# never invoked; this one proves the whole request path agrees.


def test_routes_cache_returns_same_object_until_mutation():
    app = Veloce(openapi_url=None)

    async def first(request):
        return {"ok": True}

    app.add_url_rule("/first", endpoint="first", view_func=first)
    snap1 = app.routes
    snap2 = app.routes
    assert snap1 is snap2, "cache hit should return the same list object"

    async def second(request):
        return {"ok": True}

    app.add_url_rule("/second", endpoint="second", view_func=second)
    snap3 = app.routes
    assert snap3 is not snap1, "add_url_rule must invalidate the routes cache"
    paths = {r["path"] for r in snap3}
    assert "/first" in paths and "/second" in paths


class FakeDumper:
    """Looks like a Pydantic model but isn't — `_coerce_response` should
    not route it through `JSONResponse(result.model_dump())`."""

    def model_dump(self):
        return {"oops": 1}


def test_coerce_response_does_not_duck_type_model_dump():
    app = Veloce(openapi_url=None)

    @app.get("/fake")
    async def view(request):
        return FakeDumper()

    client = app.test_client()
    resp = client.get("/fake")
    # FakeDumper is not JSON-serializable and not a Pydantic model. The
    # framework must NOT silently invoke `.model_dump()` and produce
    # `{"oops": 1}`. The only acceptable outcomes are a non-200 (orjson
    # TypeError surfacing as a 500) or a fallback body that does not
    # contain the duck-typed dump.
    assert b'"oops"' not in resp.body, (
        f"_coerce_response duck-typed .model_dump(); body={resp.body!r}"
    )
