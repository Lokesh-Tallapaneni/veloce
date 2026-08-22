"""Compiled resolver for no-wave dependency graphs (`compile_graph_resolver`).

A linear `Depends` chain - no parallel-safe batching, no Security scopes, no
`yield`-teardown, no body / async markers - compiles to a straight-line
`async` resolver. Plans with waves, scopes, teardown, active overrides, or MCP
context fall back to the interpreter. These tests assert the compiled path is
taken where expected, rejected where not, and that both paths agree.
"""

from __future__ import annotations

import pytest

from veloce import BackgroundTasks, Depends, Query, Response, Security, Veloce
from veloce._constants import STATE_INJECTED_RESPONSE
from veloce._handler_plan import build_plan
from veloce._internal import offload
from veloce._resolver_codegen import compile_graph_resolver
from veloce.dependency import _NOT_COMPILABLE, DependencyResolver, _coerce_value
from veloce.exceptions import RequestValidationError
from veloce.http.request import Request
from veloce.security import APIKeyHeader


def _req(query: str = "", headers: dict | None = None, path: str = "/x") -> Request:
    return Request(method="GET", path=path, query_string=query, headers=headers or {}, body=b"")


def _compiles(handler) -> bool:
    return (
        compile_graph_resolver(
            build_plan(handler),
            _coerce_value,
            RequestValidationError,
            offload,
            BackgroundTasks,
            Response,
        )
        is not None
    )


# ── Compilability decisions ──────────────────────────────────────────


def test_linear_chain_compiles():
    def c3():
        return 3

    def c2(x=Depends(c3)):
        return x + 1

    def c1(x=Depends(c2)):
        return x + 1

    async def handler(a=Depends(c1)):
        return a

    assert _compiles(handler)


def test_parallel_wave_does_not_compile():
    # Two independent async deps batch into one wave the interpreter runs with
    # asyncio.gather; a sequential compile would regress that, so it falls back.
    async def aa():
        return 1

    async def bb():
        return 2

    async def handler(a=Depends(aa), b=Depends(bb)):
        return (a, b)

    plan = build_plan(handler)
    assert plan.wave_members  # the wave exists
    assert not _compiles(handler)


def test_yield_dependency_does_not_compile():
    def ydep():
        yield 1

    async def handler(x=Depends(ydep)):
        return x

    assert not _compiles(handler)


def test_scoped_security_dependency_does_not_compile():
    # A Security() carrying scopes records them on the slot's list target_type;
    # the compiler rejects it so the interpreter keeps the scope-stack semantics.
    scheme = APIKeyHeader(name="x-key")

    async def handler(key=Security(scheme, scopes=["read"])):
        return key

    assert not _compiles(handler)


def test_scopeless_security_dependency_compiles():
    # A scopeless Security() is semantically identical to Depends (no scope
    # accumulation), so it compiles like any plain dependency.
    scheme = APIKeyHeader(name="x-key", auto_error=False)

    async def handler(key=Security(scheme)):
        return key

    assert _compiles(handler)


def test_body_marker_dependency_does_not_compile():
    # A dep that reads the JSON body (await request.json()) cannot be reached
    # from the synchronous parameter emit, so the graph stays on the interpreter.
    from veloce import Body

    def needs_body(v: int = Body()):
        return v

    async def handler(x=Depends(needs_body)):
        return x

    assert not _compiles(handler)


# ── Compiled / interpreter parity ────────────────────────────────────


async def _via_interpreter(handler, request, path_params=None):
    plan = build_plan(handler)
    # Force the interpreter by resolving the plan as a route-dep (the compiled
    # fast path is only taken for the handler plan with no route deps).
    r = DependencyResolver()
    return await r._resolve_slots(plan, request, path_params or {})


async def _via_compiled(handler, request, path_params=None):
    plan = build_plan(handler)
    return await DependencyResolver().resolve_plan(plan, request, path_params or {})


async def test_compiled_matches_interpreter_chain_with_params():
    def conf(region: str = "us"):
        return region

    async def loader(c=Depends(conf)):
        return c.upper()

    async def handler(item: int = Query(), x=Depends(loader)):
        return {"item": item, "x": x}

    req = _req("region=eu&item=7")
    compiled = await _via_compiled(handler, req)
    interp = await _via_interpreter(handler, _req("region=eu&item=7"))
    assert compiled == interp == {"item": 7, "x": "EU"}


async def test_shared_dependency_resolved_once_and_identical():
    calls = []

    def base():
        calls.append(1)
        return object()

    def left(b=Depends(base)):
        return b

    def right(b=Depends(base)):
        return b

    async def handler(left_=Depends(left), right_=Depends(right)):
        return left_ is right_

    out = await _via_compiled(handler, _req())
    # `base` ran once (identity dedup) and both branches see the same object.
    assert out["left_"] is out["right_"]
    assert len(calls) == 1


# ── End-to-end through the dispatcher ────────────────────────────────


@pytest.mark.asyncio
async def test_compiled_chain_end_to_end():
    app = Veloce(debug=True, openapi_url=None)

    def settings():
        return {"v": 10}

    def doubled(s=Depends(settings)):
        return s["v"] * 2

    @app.get("/calc")
    async def calc(n: int = Query(), d=Depends(doubled)):
        return {"sum": n + d}

    resp = await app.handle_request(_req("n=5", path="/calc"))
    assert resp.status_code == 200
    assert b'"sum":25' in resp.body
    # The plan compiled to the graph fast path.
    plan = build_plan(calc)
    assert (
        compile_graph_resolver(
            plan, _coerce_value, RequestValidationError, offload, BackgroundTasks, Response
        )
        is not None
    )


@pytest.mark.asyncio
async def test_override_falls_back_and_applies():
    # With an active override the compiled body (which bakes in the original
    # callable) must not be used; the interpreter applies the override instead.
    app = Veloce(debug=True, openapi_url=None)

    def flag():
        return "real"

    def wrap(f=Depends(flag)):
        return f

    @app.get("/f")
    async def f(v=Depends(wrap)):
        return {"v": v}

    app.dependency_overrides[flag] = lambda: "overridden"
    resp = await app.handle_request(_req(path="/f"))
    assert b'"v":"overridden"' in resp.body


@pytest.mark.asyncio
async def test_offload_dependency_through_compiled_path():
    # A sync dependency marked offload=True runs through the thread pool; the
    # compiled body emits `await offload(...)` for it, same result as inline.
    app = Veloce(debug=True, openapi_url=None)

    def blocking():
        return 42

    @app.get("/o")
    async def o(v=Depends(blocking, offload=True)):
        return {"v": v}

    resp = await app.handle_request(_req(path="/o"))
    assert resp.status_code == 200
    assert b'"v":42' in resp.body


def test_compiled_resolver_cached_on_plan():
    def dep():
        return 1

    async def handler(x=Depends(dep)):
        return x

    plan = build_plan(handler)
    assert plan.compiled_graph_resolver is None
    # Resolving once compiles and caches the resolver on the plan.
    import asyncio

    asyncio.new_event_loop().run_until_complete(DependencyResolver().resolve_plan(plan, _req(), {}))
    assert plan.compiled_graph_resolver is not None
    assert plan.compiled_graph_resolver is not _NOT_COMPILABLE


# ── The injected `Response` slot: emitted inline, read back by the dispatcher ──
#
# `K_RESPONSE` is the one slot the emitter restates rather than delegates: a
# helper call per request is what this compiler exists to remove (measured 19.7%
# slower warm, 10.6% cold). The emitted body and
# `DependencyResolver._bind_injected_response` therefore have to be pinned
# against each other, and both against the key `_build_response` reads.


def test_response_slot_with_a_dependency_compiles():
    async def handler(response: Response, v=Depends(lambda: 1)):
        return v

    assert _compiles(handler) is True


@pytest.mark.asyncio
async def test_compiled_and_interpreted_paths_store_under_the_same_key():
    """Both bind the injected Response into `request._state` under
    `STATE_INJECTED_RESPONSE` - the constant `app/dispatch.py` reads."""

    def dep():
        return 1

    async def compiled(response: Response, v=Depends(dep)):
        return response

    async def interpreted(response: Response):
        return response

    resolver = DependencyResolver()
    for handler in (compiled, interpreted):
        request = _req()
        kwargs = await resolver.resolve_plan(build_plan(handler), request, {})
        assert request._state[STATE_INJECTED_RESPONSE] is kwargs["response"]
        assert kwargs["response"].status_code == 0  # the "handler never set it" sentinel

    # ...and the compiled path really was the compiled path.
    assert build_plan(compiled).compiled_graph_resolver is None  # a fresh plan
    plan = build_plan(compiled)
    await resolver.resolve_plan(plan, _req(), {})
    assert plan.compiled_graph_resolver is not None
    assert plan.compiled_graph_resolver is not _NOT_COMPILABLE


@pytest.mark.asyncio
async def test_compiled_path_hands_one_response_to_dependency_and_handler():
    """The emitted body reuses an existing entry rather than overwriting it, so
    a dependency that already injected `Response` shares the handler's object."""
    seen: list[Response] = []

    def stamp(response: Response):
        seen.append(response)
        response.headers["X-From-Dep"] = "yes"

    async def handler(response: Response, _=Depends(stamp)):
        return response

    request = _req()
    kwargs = await DependencyResolver().resolve_plan(build_plan(handler), request, {})
    assert len(seen) == 1
    assert seen[0] is kwargs["response"]
    assert kwargs["response"].headers["X-From-Dep"] == "yes"


@pytest.mark.asyncio
async def test_compiled_response_injection_merges_end_to_end():
    """Through the full dispatch: the status the handler set on the compiled
    path's injected Response reaches the wire."""
    app = Veloce(debug=True, openapi_url=None)

    def dep():
        return "d"

    @app.get("/teapot")
    async def teapot(response: Response, v=Depends(dep)):
        response.status_code = 418
        response.headers["X-Injected"] = v
        return {"ok": True}

    resp = await app.handle_request(_req(path="/teapot"))
    assert resp.status_code == 418
    assert resp.headers["X-Injected"] == "d"
    assert _compiles(teapot) is True


@pytest.mark.asyncio
async def test_compiled_path_untouched_response_leaves_the_sentinel_unmerged():
    """`status_code = 0` must never surface; the dispatcher treats it as
    "handler never set it"."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/plain")
    async def plain(response: Response, v=Depends(lambda: 1)):
        return {"ok": True}

    resp = await app.handle_request(_req(path="/plain"))
    assert resp.status_code == 200
