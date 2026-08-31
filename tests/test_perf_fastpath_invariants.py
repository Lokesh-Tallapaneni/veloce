"""The dispatch fast paths are still the paths taken.

The coverage audit asked for more performance regression guards, noting that
`test_perf_dispatch.py` holds one test. It does - and deliberately: that file is
wall-clock, marked `perf`, and excluded from the default run because timing
assertions are flaky under suite contention. Adding more of those would add
flakiness, not coverage.

The regressions worth guarding are structural, and they are silent. Every
optimisation on the dispatch path works by *not* doing something, so when one
stops applying nothing fails - the request is served correctly, just slower, and
no test notices. A handler that quietly falls off the compiled resolver onto the
interpreter is the clearest case: same answers, several microseconds more, no
signal.

So these assert which path a request takes, never how long it takes. They run in
the default suite, and cost nothing to run.

Each corresponds to an optimisation that exists in the tree, with the shape of
the regression it would catch:

  compiled resolver      a param-only handler stops compiling -> interpreter
  trivial plan           a no-argument handler starts allocating a resolver
  request-only plan      a `(request)` handler stops taking its own branch
  inlined coercion       a `str` parameter goes back through the helper
  dump kwargs            response-model options rebuilt per response
  graph resolver         a linear `Depends` chain stops compiling
"""

from __future__ import annotations

import builtins
import enum
import json
import linecache

from pydantic import BaseModel

from tests.conftest import make_request
from veloce import Depends, Query, Request, Veloce
from veloce._handler_plan import build_plan
from veloce._resolver_codegen import compile_param_resolver
from veloce.dependency import _coerce_value
from veloce.exceptions import RequestValidationError
from veloce.testclient import TestClient


class Colour(enum.Enum):
    """Module scope: this file uses PEP 563, so an enum defined inside a test
    cannot be resolved by name and the slot would fall back to `str`."""

    RED = "red"


class Item(BaseModel):
    id: int
    name: str
    note: str | None = None


def _route(app: Veloce, template: str):
    for _method, _path, info in app._collect_all_routes(include_hidden=True):
        if info.path_template == template:
            return info
    raise AssertionError(f"no route for {template}")


# ── plan classification: the two branches that skip the resolver ─────


def test_a_no_argument_handler_keeps_a_trivial_plan():
    """A trivial plan returns `{}` without allocating a DependencyResolver."""
    app = Veloce(openapi_url=None)

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    assert _route(app, "/ping").is_trivial_plan is True


def test_a_request_only_handler_keeps_its_own_branch():
    app = Veloce(openapi_url=None)

    @app.get("/echo")
    async def echo(request: Request) -> dict:
        return {"path": request.path}

    info = _route(app, "/echo")
    assert info.is_request_only_plan is True
    assert info.is_trivial_plan is False


def test_a_parameterised_handler_is_neither():
    """The negative: these flags must not be set for a handler that binds."""
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def items(q: str = "x") -> dict:
        return {"q": q}

    info = _route(app, "/items")
    assert info.is_trivial_plan is False
    assert info.is_request_only_plan is False


# ── the compiled resolver still compiles what it used to ─────────────


def test_a_scalar_param_handler_compiles():
    async def handler(q: int = 0, name: str = "x"):
        return q

    assert compile_param_resolver(build_plan(handler), _coerce_value, RequestValidationError)


def test_a_marker_handler_compiles():

    async def handler(q: int = Query(gt=0)):
        return q

    assert compile_param_resolver(build_plan(handler), _coerce_value, RequestValidationError)


def test_a_path_param_handler_compiles():
    async def handler(item_id: int):
        return item_id

    assert compile_param_resolver(build_plan(handler), _coerce_value, RequestValidationError)


def test_a_dependency_handler_does_not_compile():
    """The negative: the compiler must keep rejecting what it cannot reproduce."""

    def dep() -> int:
        return 1

    async def handler(q: int = 0, d: int = Depends(dep)):
        return q

    assert (
        compile_param_resolver(build_plan(handler), _coerce_value, RequestValidationError) is None
    )


# ── coercion stays inlined in the generated source ───────────────────


def _generated(handler) -> str:
    resolver = compile_param_resolver(build_plan(handler), _coerce_value, RequestValidationError)
    assert resolver is not None
    return "".join(linecache.getlines(resolver.__code__.co_filename))


def test_a_str_parameter_is_read_without_a_coercion_call():
    async def handler(q: str = "d"):
        return q

    assert "_cv(" not in _generated(handler)


def test_an_int_parameter_converts_inline():
    async def handler(q: int = 0):
        return q

    assert "int(" in _generated(handler)


def test_an_enum_parameter_still_uses_the_helper():
    """The negative: not everything is inlined, and that is deliberate."""

    async def handler(c: Colour = Colour.RED):
        return c

    assert "_cv(" in _generated(handler)


# ── response-model options stay resolved at registration ─────────────


def test_the_dump_options_are_precomputed():
    app = Veloce(openapi_url=None)

    @app.get("/i", response_model=Item, response_model_exclude_none=True)
    async def read() -> Item:
        return Item(id=1, name="a")

    assert _route(app, "/i").response_dump_kwargs == {"exclude_none": True}


def test_a_route_without_options_precomputes_an_empty_mapping():
    app = Veloce(openapi_url=None)

    @app.get("/i", response_model=Item)
    async def read() -> Item:
        return Item(id=1, name="a")

    assert _route(app, "/i").response_dump_kwargs == {}


# ── the graph resolver still compiles a linear chain ─────────────────


def test_a_linear_dependency_chain_compiles_on_first_use():
    """Built by the resolver on first use, so serving the route compiles it."""

    async def dep(q: int = 0) -> int:
        return q

    app = Veloce(openapi_url=None)

    @app.get("/chained")
    async def chained(value: int = Depends(dep)) -> dict:
        return {"value": value}

    with TestClient(app) as client:
        assert client.get("/chained?q=3").json() == {"value": 3}

    plan = _route(app, "/chained").handler_plan
    assert callable(plan.compiled_graph_resolver)


# ── the request path imports nothing ─────────────────────────────────


def test_no_import_happens_while_serving_a_request():
    """A per-request import is a `sys.modules` lookup on every call, and the
    kind of regression that arrives with an innocent-looking helper."""
    app = Veloce(openapi_url=None)

    @app.get("/plain")
    async def plain(q: str = "x") -> dict:
        return {"q": q}

    client = TestClient(app)
    client.get("/plain")  # warm every lazy path first

    imported: list[str] = []
    real_import = builtins.__import__

    def tracking_import(name, *args, **kwargs):
        imported.append(name)
        return real_import(name, *args, **kwargs)

    builtins.__import__ = tracking_import
    try:
        client.get("/plain?q=abc")
    finally:
        builtins.__import__ = real_import

    assert imported == []


# ── trivial-route executor classification ────────────────────
#
# Moved here from `test_app.py`, where these sat in a bare-function tail whose
# sections were labelled by internal batch id (`S7:`, `P-6:`).


async def test_trivial_route_classified_and_dispatches():
    """A handler with no injected parameters is classified trivial and is
    dispatched without entering the dependency resolver."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/trivial")
    async def trivial():
        return {"ok": True}

    @app.get("/with-request")
    async def with_request(request: Request):
        return {"seen": request.path}

    @app.get("/with-param/{n}")
    async def with_param(n: int):
        return {"n": n}

    assert app.match("GET", "/trivial").route_info.is_trivial_plan is True
    assert app.match("GET", "/with-request").route_info.is_trivial_plan is False
    assert app.match("GET", "/with-param/5").route_info.is_trivial_plan is False

    # All three still dispatch correctly.
    assert (await app.handle_request(make_request(path="/trivial"))).status_code == 200
    assert (await app.handle_request(make_request(path="/with-request"))).status_code == 200
    param_resp = await app.handle_request(make_request(path="/with-param/5"))
    assert param_resp.status_code == 200
    assert json.loads(param_resp.body) == {"n": 5}


async def test_route_with_dependency_is_not_trivial():
    """A route-level dependency keeps the route on the full resolve path."""

    async def dep():
        return "x"

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/d", dependencies=[Depends(dep)])
    async def d():
        return {"ok": True}

    assert app.match("GET", "/d").route_info.is_trivial_plan is False
    assert (await app.handle_request(make_request(path="/d"))).status_code == 200


async def test_paramless_route_under_app_level_dependency_is_not_trivial():
    """An app-level `Veloce(dependencies=...)` keeps even a parameter-less
    handler on the full resolve path, so the dependency still runs."""
    ran: list[bool] = []

    async def dep():
        ran.append(True)
        return "x"

    app = Veloce(debug=True, openapi_url=None, dependencies=[Depends(dep)])

    @app.get("/d")
    async def d():
        return {"ok": True}

    assert app.match("GET", "/d").route_info.is_trivial_plan is False
    resp = await app.handle_request(make_request(path="/d"))
    assert resp.status_code == 200
    assert ran == [True]  # the app-level dependency actually executed
