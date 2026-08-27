"""DI correctness for scope-aware caching, teardown aggregation, and the
registration-time kwarg-ambiguity guard.

Covers three competitive-audit findings against the dependency-injection path:

* Scope-aware dependency cache - a `Security()` dependency whose sub-graph reads
  `SecurityScopes` must resolve as distinct cached entries when referenced with
  different scope sets, while plain `Depends` keeps its single-call cache.
* Teardown exception aggregation - `run_teardowns` runs every teardown in
  reverse even when one fails, then re-raises the failures together so a broken
  teardown is observable instead of silently swallowed.
* Kwarg-ambiguity validation - a by-name magic parameter (`request`, `ws`)
  carrying a conflicting value marker is rejected at registration, with no false
  positives for ordinary handlers.
"""

from __future__ import annotations

import builtins
import sys

import pytest

from tests.conftest import make_request
from veloce import (
    ConfigurationError,
    Depends,
    Query,
    Request,
    Security,
    SecurityScopes,
    Veloce,
)
from veloce.dependency import DependencyResolver


def _req(path: str = "/", query: str = "") -> Request:
    return make_request(method="GET", path=path, query_string=query, headers={}, body=b"")


# ── Finding 13: scope-aware dependency cache ───────────────────────────


async def test_scope_sensitive_dependency_not_collapsed_across_scope_sets():
    """The canonical repro: the same auth callable referenced with two scope
    sets in one request resolves twice, each with the right scopes."""
    app = Veloce(debug=True, openapi_url=None)
    calls: list[list[str]] = []

    def auth(security_scopes: SecurityScopes):
        calls.append(list(security_scopes.scopes))
        return tuple(security_scopes.scopes)

    @app.get("/x")
    async def x(
        a=Security(auth, scopes=["read"]),
        b=Security(auth, scopes=["read", "write"]),
    ):
        return {"a": list(a), "b": list(b)}

    resp = await app.handle_request(_req("/x"))
    assert calls == [["read"], ["read", "write"]]
    assert resp.body == b'{"a":["read"],"b":["read","write"]}'


async def test_same_scope_set_still_cached_once():
    """Two references with the SAME scope set still collapse to one call."""
    app = Veloce(debug=True, openapi_url=None)
    calls: list[int] = []

    def auth(security_scopes: SecurityScopes):
        calls.append(1)
        return "ok"

    @app.get("/x")
    async def x(
        a=Security(auth, scopes=["read"]),
        b=Security(auth, scopes=["read"]),
    ):
        return {}

    await app.handle_request(_req("/x"))
    assert len(calls) == 1


async def test_security_not_reading_scopes_still_cached_once():
    """A Security() dep that never reads `SecurityScopes` is scope-insensitive:
    different scope sets keep the cheap identity cache, so it runs once."""
    app = Veloce(debug=True, openapi_url=None)
    calls: list[int] = []

    def auth():
        calls.append(1)
        return "token"

    @app.get("/x")
    async def x(
        a=Security(auth, scopes=["read"]),
        b=Security(auth, scopes=["read", "write"]),
    ):
        return {}

    await app.handle_request(_req("/x"))
    assert len(calls) == 1


async def test_plain_depends_still_cached_once():
    """Plain `Depends` (no scopes) keeps the single-call cache untouched."""
    app = Veloce(debug=True, openapi_url=None)
    calls: list[int] = []

    def dep():
        calls.append(1)
        return 42

    @app.get("/y")
    async def y(a=Depends(dep), b=Depends(dep)):
        return {"a": a, "b": b}

    resp = await app.handle_request(_req("/y"))
    assert len(calls) == 1
    assert resp.body == b'{"a":42,"b":42}'


async def test_use_cache_false_security_runs_each_time():
    """`use_cache=False` is independent of scope sensitivity."""
    app = Veloce(debug=True, openapi_url=None)
    calls: list[list[str]] = []

    def auth(security_scopes: SecurityScopes):
        calls.append(list(security_scopes.scopes))
        return None

    @app.get("/x")
    async def x(
        a=Security(auth, scopes=["read"], use_cache=False),
        b=Security(auth, scopes=["read"], use_cache=False),
    ):
        return {}

    await app.handle_request(_req("/x"))
    assert calls == [["read"], ["read"]]


# ── Finding 14: teardown exception aggregation ─────────────────────────


async def test_run_teardowns_runs_all_then_raises():
    """Every teardown runs in reverse order even when earlier ones fail; the
    failures are re-raised together instead of being swallowed."""
    r = DependencyResolver()
    order: list[str] = []

    def g1():
        try:
            yield 1
        finally:
            order.append("t1")
            raise RuntimeError("teardown 1 failed")

    def g2():
        try:
            yield 2
        finally:
            order.append("t2")
            raise RuntimeError("teardown 2 failed")

    def g3():
        try:
            yield 3
        finally:
            order.append("t3")

    for g in (g1, g2, g3):
        gen = g()
        next(gen)
        r._teardowns.append(("sync", gen))

    # Broad by necessity: 3.11+ aggregates several teardown failures into a
    # `BaseExceptionGroup` and 3.10 raises the first with the rest chained,
    # so the type differs by interpreter. The assertions below pin the
    # content on both.
    with pytest.raises(BaseException) as ei:  # noqa: B017 - see above
        await r.run_teardowns()

    # Reverse order, and every teardown ran despite the failures.
    assert order == ["t3", "t2", "t1"]
    raised = ei.value
    if sys.version_info >= (3, 11):
        # `BaseExceptionGroup` is a builtin only on 3.11+; fetch it via the
        # builtins module so the reference does not trip ruff's py310 target.
        group_type = builtins.BaseExceptionGroup
        assert isinstance(raised, group_type)
        msgs = sorted(str(x) for x in raised.exceptions)
        assert msgs == ["teardown 1 failed", "teardown 2 failed"]
    else:  # 3.10 has no exception groups
        assert isinstance(raised, RuntimeError)


async def test_run_teardowns_clean_does_not_raise():
    """A clean teardown chain returns normally - no allocation, no raise."""
    r = DependencyResolver()
    order: list[str] = []

    def g():
        yield 1
        order.append("done")

    gen = g()
    next(gen)
    r._teardowns.append(("sync", gen))

    await r.run_teardowns()
    assert order == ["done"]


async def test_run_teardowns_does_not_double_count_request_error():
    """The request exception thrown into a teardown re-emerging unchanged is
    not aggregated as a teardown failure."""
    r = DependencyResolver()
    req_exc = ValueError("request boom")

    def g():
        # No try/finally: the thrown request exception propagates verbatim.
        yield 1

    gen = g()
    next(gen)
    r._teardowns.append(("sync", gen))

    # The request error is the caller's; run_teardowns must not re-raise it.
    await r.run_teardowns(req_exc)


async def test_run_teardowns_chains_from_request_error():
    """A genuine teardown failure during error handling chains from the
    original request exception."""
    r = DependencyResolver()
    req_exc = ValueError("request boom")

    def g():
        try:
            yield 1
        except ValueError as err:
            raise RuntimeError("cleanup failed") from err

    gen = g()
    next(gen)
    r._teardowns.append(("sync", gen))

    # Broad by necessity, as above: the aggregate's type differs by
    # interpreter. The `__cause__` assertion below is the contract.
    with pytest.raises(BaseException) as ei:  # noqa: B017 - see above
        await r.run_teardowns(req_exc)
    assert ei.value.__cause__ is req_exc


async def test_yield_dependency_teardown_failure_does_not_break_response():
    """End to end: a failing teardown is logged at the dispatcher and the
    response is still delivered intact."""
    app = Veloce(debug=True, openapi_url=None)

    def dep():
        try:
            yield "resource"
        finally:
            raise RuntimeError("teardown blew up")

    @app.get("/x")
    async def x(res=Depends(dep)):
        return {"res": res}

    resp = await app.handle_request(_req("/x"))
    assert resp.status_code == 200
    assert resp.body == b'{"res":"resource"}'


# ── Finding 22: kwarg-ambiguity validation ─────────────────────────────


def test_request_name_with_marker_rejected():
    """`request: str = Query()` is the precedence trap - flagged at startup."""
    app = Veloce(openapi_url=None)
    with pytest.raises(ConfigurationError) as ei:

        @app.get("/x")
        async def x(request: str = Query()):
            return {}

    assert "request" in str(ei.value)
    assert "Query" in str(ei.value)


def test_ws_name_with_marker_rejected():
    """On a WebSocket plan, `ws` / `websocket` name + marker is flagged."""
    app = Veloce(openapi_url=None)
    with pytest.raises(ConfigurationError):

        @app.websocket("/ws")
        async def w(ws: str = Query()):
            pass


def test_nested_dependency_ambiguity_reports_chain():
    """A nested dependency's ambiguity surfaces the dependency chain."""
    app = Veloce(openapi_url=None)

    def bad_dep(request: str = Query()):
        return request

    with pytest.raises(ConfigurationError) as ei:

        @app.get("/n")
        async def n(x=Depends(bad_dep)):
            return {}

    assert "bad_dep" in str(ei.value)


# ── No-false-positive cases ──


def test_request_typed_request_is_valid():
    app = Veloce(openapi_url=None)

    @app.get("/a")
    async def a(request: Request):
        return {}

    assert _registered(a)


def test_ordinary_query_param_is_valid():
    app = Veloce(openapi_url=None)

    @app.get("/b")
    async def b(q: str = Query()):
        return {}

    assert _registered(b)


def test_request_named_depends_is_allowed():
    """`request=Depends(...)` errs toward allowing - a dependency may be named
    `request` (only value markers conflict)."""
    app = Veloce(openapi_url=None)

    def dep():
        return 1

    @app.get("/c")
    async def c(request=Depends(dep)):
        return {}

    assert _registered(c)


def test_ws_name_in_http_plan_is_valid():
    """`ws` is magic only on a WebSocket plan; an HTTP handler may use it."""
    app = Veloce(openapi_url=None)

    @app.get("/d")
    async def d(ws: str = Query()):
        return {}

    assert _registered(d)


def _registered(handler) -> bool:
    """Registration succeeded if we reached here without ConfigurationError."""
    return handler is not None


def test_configuration_error_in_exports():
    from veloce import ConfigurationError as CE

    assert CE is ConfigurationError


async def test_nested_scope_reader_behind_plain_depends_not_collapsed():
    """A scope-reading helper reached through a plain `Depends` inside a
    `Security()` wrapper must still resolve per scope set (the wrapper inherits
    the scopes; the helper below reads them), not collapse by callable identity."""

    app = Veloce(debug=True, openapi_url=None)
    seen: list[list[str]] = []

    def auth(security_scopes: SecurityScopes):
        seen.append(list(security_scopes.scopes))
        return tuple(security_scopes.scopes)

    def wrapper(inner=Depends(auth)):  # plain Depends - no own scopes
        return inner

    @app.get("/x")
    async def x(
        a=Security(wrapper, scopes=["read"]),
        b=Security(wrapper, scopes=["read", "write"]),
    ):
        return {"a": list(a), "b": list(b)}

    await app.handle_request(_req("/x"))
    assert seen == [["read"], ["read", "write"]]
