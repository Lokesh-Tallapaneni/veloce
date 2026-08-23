"""Topological dependency batching - independent deps batch across slots.

The resolver batches every parallel-safe dependency regardless of declaration
order, intervening non-dependency parameters, or nesting depth - so two
independent async deps separated by a `Query()` slot still run concurrently.
Batching is computed once at registration (`compute_dep_waves`) and must be
semantics-preserving: identical kwargs, identical teardown order, single
invocation of a shared cached dependency, and no scope-stack corruption.
"""

from __future__ import annotations

import asyncio
import time

from veloce import Depends, Query, Security, Veloce
from veloce._handler_plan import build_plan, compute_dep_waves
from veloce.dependency import SecurityScopes
from veloce.testclient import TestClient

#: How long each probe dependency sleeps. A sequential resolver makes the
#: second dependency start a whole delay behind the first; a concurrent one
#: starts both at once.
_DEP_DELAY = 0.05

# ── Wave computation (registration-time) ──────────────────────────────


def test_deps_separated_by_query_still_batch():
    """The case the contiguous heuristic missed: a Query() between two deps."""

    def a():
        return 1

    def b():
        return 2

    async def h(x: int = Depends(a), q: int = Query(default=0), y: int = Depends(b)):
        return x + y + q

    plan = build_plan(h)
    # Slots: 0=Depends(a), 1=Query, 2=Depends(b). The two deps batch despite
    # the Query slot between them; the legacy contiguous map cannot.
    assert plan.dep_waves == [[0, 2]]
    assert plan.parallel_groups == {}


def test_three_independent_deps_one_wave():
    def a():
        return 1

    def b():
        return 2

    def c():
        return 3

    async def h(
        x: int = Depends(a),
        p: int = Query(default=0),
        y: int = Depends(b),
        z: int = Depends(c),
    ):
        return x + y + z

    assert build_plan(h).dep_waves == [[0, 2, 3]]


def test_security_dep_excluded_from_waves():
    def a():
        return 1

    def guard():
        return "ok"

    async def h(x: int = Depends(a), s: str = Security(guard, scopes=["read"])):
        return x

    # Only one parallel-safe dep remains -> nothing to batch.
    assert build_plan(h).dep_waves == []


def test_yield_dep_excluded_from_waves():
    def a():
        return 1

    def res():
        yield "r"

    async def h(x: int = Depends(a), r: str = Depends(res)):
        return x

    assert build_plan(h).dep_waves == []


def test_shared_cached_dep_split_into_successive_waves():
    """Two siblings sharing a cached callable land in different waves."""

    def shared():
        return "s"

    def wrap_a(v: str = Depends(shared)):
        return f"a:{v}"

    def wrap_b(v: str = Depends(shared)):
        return f"b:{v}"

    async def h(x: str = Depends(wrap_a), y: str = Depends(wrap_b)):
        return x + y

    waves = build_plan(h).dep_waves
    # wrap_a and wrap_b both reach the cached `shared` callable, so they cannot
    # share a wave. With nothing left to run concurrently, no batching is
    # emitted and the resolver runs them inline in slot order - the shared
    # callable's cache de-dup (verified at runtime below) keeps it single-call.
    assert waves == []


def test_two_waves_form_when_cache_prerequisite_splits_a_batch():
    """A shared cached dep splits an otherwise-parallel batch into two waves."""

    def shared():
        return "s"

    def free():
        return "f"

    def wrap_a(v: str = Depends(shared)):
        return f"a:{v}"

    def wrap_b(v: str = Depends(shared)):
        return f"b:{v}"

    async def h(
        x: str = Depends(wrap_a),
        z: str = Depends(free),
        y: str = Depends(wrap_b),
    ):
        return x + y + z

    # Slots 0=wrap_a, 1=free, 2=wrap_b. wrap_a and free are disjoint so they
    # share wave 0; wrap_b shares the cached `shared` callable with wrap_a, so
    # it is pushed to wave 1 - which then runs after wave 0 fills the cache.
    assert build_plan(h).dep_waves == [[0, 1], [2]]


def test_single_dep_yields_no_waves():
    def a():
        return 1

    async def h(x: int = Depends(a)):
        return x

    assert build_plan(h).dep_waves == []


def test_compute_dep_waves_pure_function_of_slots():
    def a():
        return 1

    def b():
        return 2

    async def h(x: int = Depends(a), y: int = Depends(b)):
        return x

    plan = build_plan(h)
    assert plan.dep_waves == compute_dep_waves(plan.slots)


# ── Runtime semantics ─────────────────────────────────────────────────


def test_interleaved_deps_run_concurrently():
    """Two deps separated by a Query slot start within a tiny window."""
    app = Veloce(openapi_url=None)
    starts: list[float] = []

    async def slow_a() -> str:
        starts.append(time.monotonic())
        await asyncio.sleep(_DEP_DELAY)
        return "a"

    async def slow_b() -> str:
        starts.append(time.monotonic())
        await asyncio.sleep(_DEP_DELAY)
        return "b"

    @app.get("/interleaved")
    async def handler(
        a: str = Depends(slow_a),
        q: str = Query(default="q"),
        b: str = Depends(slow_b),
    ) -> dict:
        return {"a": a, "b": b, "q": q}

    resp = TestClient(app).get("/interleaved")
    assert resp.status_code == 200
    assert resp.json() == {"a": "a", "b": "b", "q": "q"}
    assert len(starts) == 2
    # Concurrent start: the second begins promptly, not a whole `_DEP_DELAY`
    # later. Half that delay still fails a sequential implementation by a wide
    # margin while tolerating a loaded scheduler.
    assert abs(starts[1] - starts[0]) < _DEP_DELAY / 2, (
        f"interleaved deps did not start concurrently: delta={starts[1] - starts[0]:.4f}s"
    )


def test_shared_cached_dep_invoked_once_across_waves():
    app = Veloce(openapi_url=None)
    calls = 0

    async def shared() -> str:
        nonlocal calls
        calls += 1
        return "s"

    async def wrap_a(v: str = Depends(shared)) -> str:
        return f"a:{v}"

    async def wrap_b(v: str = Depends(shared)) -> str:
        return f"b:{v}"

    @app.get("/cached")
    async def handler(x: str = Depends(wrap_a), y: str = Depends(wrap_b)) -> dict:
        return {"x": x, "y": y}

    resp = TestClient(app).get("/cached")
    assert resp.status_code == 200
    assert resp.json() == {"x": "a:s", "y": "b:s"}
    # The shared cached dep is filled in the first wave and reused in the
    # second - never invoked twice.
    assert calls == 1


def test_two_wave_runtime_fills_then_reuses_cache():
    """The multi-wave path runs wave 0 fully before wave 1 reuses its cache."""
    app = Veloce(openapi_url=None)
    calls = 0

    async def shared() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "s"

    async def free() -> str:
        return "f"

    async def wrap_a(v: str = Depends(shared)) -> str:
        return f"a:{v}"

    async def wrap_b(v: str = Depends(shared)) -> str:
        return f"b:{v}"

    @app.get("/twowave")
    async def handler(
        x: str = Depends(wrap_a),
        z: str = Depends(free),
        y: str = Depends(wrap_b),
    ) -> dict:
        return {"x": x, "y": y, "z": z}

    resp = TestClient(app).get("/twowave")
    assert resp.status_code == 200
    assert resp.json() == {"x": "a:s", "y": "b:s", "z": "f"}
    # `shared` runs once (wave 0 via wrap_a), reused by wrap_b in wave 1.
    assert calls == 1


def test_yield_teardown_order_preserved_with_interleaved_deps():
    """Yield deps stay inline in slot order even alongside batched deps."""
    app = Veloce(openapi_url=None)
    events: list[str] = []

    async def res_one():
        events.append("enter1")
        yield "1"
        events.append("exit1")

    async def res_two():
        events.append("enter2")
        yield "2"
        events.append("exit2")

    async def plain() -> str:
        events.append("plain")
        return "p"

    @app.get("/teardown")
    async def handler(
        a: str = Depends(res_one),
        p: str = Depends(plain),
        b: str = Depends(res_two),
    ) -> dict:
        events.append("handler")
        return {"a": a, "b": b, "p": p}

    resp = TestClient(app).get("/teardown")
    assert resp.status_code == 200
    # Setup runs in slot order; teardown unwinds in reverse.
    assert events == [
        "enter1",
        "plain",
        "enter2",
        "handler",
        "exit2",
        "exit1",
    ]


def test_security_scopes_resolve_correctly_with_batched_deps():
    """A Security() chain alongside batched plain deps keeps correct scopes."""
    app = Veloce(openapi_url=None)

    def reader(scopes: SecurityScopes) -> list[str]:
        return list(scopes.scopes)

    async def a() -> str:
        return "a"

    async def b() -> str:
        return "b"

    @app.get("/scoped")
    async def handler(
        x: str = Depends(a),
        s: list = Security(reader, scopes=["read", "write"]),
        y: str = Depends(b),
    ) -> dict:
        return {"x": x, "y": y, "scopes": s}

    resp = TestClient(app).get("/scoped")
    assert resp.status_code == 200
    body = resp.json()
    assert body["x"] == "a"
    assert body["y"] == "b"
    assert sorted(body["scopes"]) == ["read", "write"]


def test_exception_from_batched_dep_propagates():
    app = Veloce(openapi_url=None)

    async def ok() -> str:
        return "ok"

    async def boom() -> str:
        raise RuntimeError("dep failed")

    @app.get("/boom")
    async def handler(x: str = Depends(ok), y: str = Depends(boom)) -> dict:
        return {"x": x, "y": y}

    resp = TestClient(app).get("/boom")
    # The failure surfaces as a 500, not a swallowed result.
    assert resp.status_code == 500
