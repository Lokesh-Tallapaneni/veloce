"""Tests for the param-only compiled resolver (veloce._resolver_codegen)."""

from __future__ import annotations

from veloce import Depends, Query, TestClient, Veloce
from veloce._handler_plan import build_plan
from veloce._resolver_codegen import compile_param_resolver
from veloce.dependency import _coerce_value
from veloce.exceptions import RequestValidationError


def _compile(handler):
    return compile_param_resolver(build_plan(handler), _coerce_value, RequestValidationError)


def _q_errors(resp):
    # The validation-error detail entries that concern the `q` parameter.
    return [e for e in resp.json()["detail"] if "q" in (e.get("loc") or [])]


def test_422_payload_parity_compiled_vs_interpreter():
    # Same missing/invalid `q` semantics on a compiled handler (params only)
    # and an interpreter handler (a Depends forces the fallback). The 422
    # bodies for `q` must be identical so error shapes do not drift between
    # the two code paths on the same app.
    def _dep():
        return 1

    app = Veloce(openapi_url=None)

    @app.get("/compiled")
    async def compiled(q: int):
        return {"q": q}

    @app.get("/interp")
    async def interp(q: int, _d: int = Depends(_dep)):
        return {"q": q}

    client = TestClient(app)
    # Both paths must actually be what we think they are.
    assert _compile(compiled) is not None  # compiled path
    assert _compile(interp) is None  # falls back to interpreter

    # Missing required.
    assert _q_errors(client.get("/compiled")) == _q_errors(client.get("/interp"))
    # Invalid coercion.
    assert _q_errors(client.get("/compiled?q=x")) == _q_errors(client.get("/interp?q=x"))


def test_compiles_request_only_handler():
    async def h(request):
        return None

    assert _compile(h) is not None


def test_compiles_scalar_path_and_query_handler():
    async def h(x: int, y: str = "d"):
        return None

    assert _compile(h) is not None


def test_does_not_compile_dependency_handler():
    def dep():
        return 1

    async def h(x: int, d: int = Depends(dep)):
        return None

    assert _compile(h) is None


def test_does_not_compile_marker_handler():
    async def h(q: int = Query(gt=0)):
        return None

    assert _compile(h) is None


def test_does_not_compile_list_param_handler():
    async def h(tags: list[str]):
        return None

    assert _compile(h) is None


def test_coercion_and_defaults_end_to_end():
    app = Veloce(openapi_url=None)

    @app.get("/m/{x:int}/{y:int}")
    async def multi(x: int, y: int, a: str = "a", b: int = 0):
        return {"x": x, "y": y, "a": a, "b": b}

    client = TestClient(app)
    assert client.get("/m/3/4?a=hi&b=7").json() == {"x": 3, "y": 4, "a": "hi", "b": 7}
    # Defaults apply when query params are absent.
    assert client.get("/m/1/2").json() == {"x": 1, "y": 2, "a": "a", "b": 0}


def test_optional_param_resolves_to_none():
    app = Veloce(openapi_url=None)

    @app.get("/opt")
    async def opt(q: int | None = None):
        return {"q": q}

    assert TestClient(app).get("/opt").json() == {"q": None}


def test_missing_required_query_is_422():
    app = Veloce(openapi_url=None)

    @app.get("/need")
    async def need(q: int):
        return {"q": q}

    assert TestClient(app).get("/need").status_code == 422


def test_invalid_query_coercion_is_422():
    app = Veloce(openapi_url=None)

    @app.get("/c")
    async def c(n: int = 0):
        return {"n": n}

    assert TestClient(app).get("/c?n=notanint").status_code == 422


def test_path_value_takes_precedence_over_query():
    app = Veloce(openapi_url=None)

    @app.get("/p/{name}")
    async def p(name: str):
        return {"name": name}

    # The path binding wins over a same-named query param, matching the
    # interpreter's path-or-query resolution order.
    assert TestClient(app).get("/p/frompath?name=fromquery").json() == {"name": "frompath"}


def test_compiled_resolver_is_cached_on_plan():
    async def h(x: int):
        return None

    plan = build_plan(h)
    assert plan.compiled_resolver is None  # not yet attempted
    from veloce.dependency import DependencyResolver
    from veloce.http.request import Request

    resolver = DependencyResolver()
    req = Request(method="GET", path="/", query_string="x=5", headers=[], body=b"")

    import asyncio

    kwargs = asyncio.new_event_loop().run_until_complete(resolver.resolve_plan(plan, req, {}))
    assert kwargs == {"x": 5}
    # After first use the compiled function is cached on the plan.
    assert callable(plan.compiled_resolver)


def test_reused_resolver_clears_state_before_compiled_path():
    # DependencyResolver is public and may be reused across resolves. A prior
    # resolve that registered a yield-style teardown must NOT leak into a later
    # compiled param-only resolve — the compiled fast path must still reset().
    import asyncio

    from veloce.dependency import DependencyResolver
    from veloce.http.request import Request

    def yielder():
        yield "x"  # yield dep → registers a teardown on the resolver

    async def with_dep(d: str = Depends(yielder)):
        return d

    async def params_only(q: int):
        return q

    resolver = DependencyResolver()
    req = Request(method="GET", path="/", query_string="q=5", headers=[], body=b"")

    async def run():
        await resolver.resolve_plan(build_plan(with_dep), req, {})
        assert resolver._teardowns, "yield dep should have registered a teardown"
        kwargs = await resolver.resolve_plan(build_plan(params_only), req, {})
        assert kwargs == {"q": 5}
        # The compiled fast path must have reset() first, clearing A's teardown.
        assert resolver._teardowns == [], "stale teardown leaked into the compiled path"

    asyncio.new_event_loop().run_until_complete(run())
