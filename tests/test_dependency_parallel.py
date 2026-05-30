"""Sibling `Depends()` slots resolve concurrently when safe (CL10).

When a handler declares multiple independent `Depends()` parameters,
the resolver runs them under `asyncio.gather` instead of awaiting them
one at a time. The win shows up clearly when each dependency does I/O
(an `asyncio.sleep` here stands in for any awaitable wait): the total
resolve time becomes `max(dep_durations)` rather than `sum(...)`.

Constraints — preserved by `_parallel_dep_group_end`:
- Security() scope-pushing dependencies stay sequential.
- yield-style dependencies stay sequential.
- two siblings sharing a `use_cache=True` callable stay sequential.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from veloce import Depends, Veloce
from veloce.testclient import TestClient


def test_independent_async_siblings_run_in_parallel():
    """Two sibling Depends() begin concurrently rather than sequentially.

    The robust assertion is the **start-time delta**, not total elapsed
    — under a noisy CI scheduler total elapsed jitters but the start
    delta between two concurrently-scheduled coroutines stays small
    even when each individual coroutine takes longer than expected.
    """
    app = Veloce(openapi_url=None)
    starts: list[float] = []

    async def slow_a() -> str:
        starts.append(time.monotonic())
        await asyncio.sleep(0.05)
        return "a"

    async def slow_b() -> str:
        starts.append(time.monotonic())
        await asyncio.sleep(0.05)
        return "b"

    @app.get("/parallel")
    async def handler(a: str = Depends(slow_a), b: str = Depends(slow_b)) -> dict:
        return {"a": a, "b": b}

    resp = TestClient(app).get("/parallel")
    assert resp.status_code == 200
    assert resp.json() == {"a": "a", "b": "b"}
    assert len(starts) == 2
    # Both deps must begin within a tiny window of each other —
    # sequential would mean the second waits ~50 ms behind the first.
    assert abs(starts[1] - starts[0]) < 0.010, (
        f"siblings did not start concurrently: delta={starts[1] - starts[0]:.4f}s"
    )


def test_shared_use_cache_dependency_runs_once():
    """A dependency reused by two siblings with `use_cache=True` must
    not be invoked twice — parallelisation falls back to sequential
    when that would race the cache.
    """
    app = Veloce(openapi_url=None)
    call_count = 0

    async def shared() -> str:
        nonlocal call_count
        call_count += 1
        return "shared"

    @app.get("/shared")
    async def handler(a: str = Depends(shared), b: str = Depends(shared)) -> dict:
        return {"a": a, "b": b}

    resp = TestClient(app).get("/shared")
    assert resp.status_code == 200
    assert resp.json() == {"a": "shared", "b": "shared"}
    # Cache hit: invoked once across siblings.
    assert call_count == 1


def test_yield_dependency_still_tears_down_in_order():
    """yield-style deps stay on the sequential path. Their teardowns
    fire in registration order regardless of any parallel sibling."""
    app = Veloce(openapi_url=None)
    events: list[str] = []

    async def yield_dep():
        events.append("enter")
        yield "from-yield"
        events.append("exit")

    async def plain_dep() -> str:
        events.append("plain")
        return "plain"

    @app.get("/mixed")
    async def handler(
        y: str = Depends(yield_dep),
        p: str = Depends(plain_dep),
    ) -> dict:
        events.append("handler")
        return {"y": y, "p": p}

    resp = TestClient(app).get("/mixed")
    assert resp.status_code == 200
    assert resp.json() == {"y": "from-yield", "p": "plain"}
    # Yield enters before plain (sequential), handler runs, yield exits last.
    assert events == ["enter", "plain", "handler", "exit"]


@pytest.mark.asyncio
async def test_group_end_helper_stops_at_security_dependency():
    """`_parallel_dep_group_end` refuses to expand past a Security() slot."""
    from types import SimpleNamespace

    from veloce.dependency import K_DEPENDS, DependencyResolver

    plain = SimpleNamespace(
        kind=K_DEPENDS,
        target_type=None,
        dep_is_gen=False,
        dep_is_async_gen=False,
        use_cache=False,
        dep_callable=lambda: None,
        sub_plan=None,
    )
    sec = SimpleNamespace(
        kind=K_DEPENDS,
        target_type=["read"],  # scope list — Security() shape
        dep_is_gen=False,
        dep_is_async_gen=False,
        use_cache=False,
        dep_callable=lambda: None,
        sub_plan=None,
    )
    resolver = DependencyResolver()
    # plain, plain, sec, plain — the run should stop at the Security() slot.
    end = resolver._parallel_dep_group_end([plain, plain, sec, plain], 0)
    assert end == 2


@pytest.mark.asyncio
async def test_group_end_helper_refuses_nested_security():
    """An outer plain `Depends` whose sub_plan transitively contains
    a Security() slot must not be parallelised — the shared
    `_scope_stack` would otherwise be corrupted by interleaved
    push/pop pairs across sibling tasks.
    """
    from types import SimpleNamespace

    from veloce.dependency import K_DEPENDS, DependencyResolver

    # An inner Security slot reachable through outer plain's sub_plan.
    inner_sec = SimpleNamespace(
        kind=K_DEPENDS,
        target_type=["s1"],
        dep_is_gen=False,
        dep_is_async_gen=False,
        use_cache=False,
        dep_callable=lambda: None,
        sub_plan=None,
    )
    outer_with_nested_sec = SimpleNamespace(
        kind=K_DEPENDS,
        target_type=None,  # plain at the outer level...
        dep_is_gen=False,
        dep_is_async_gen=False,
        use_cache=False,
        dep_callable=lambda: None,
        sub_plan=SimpleNamespace(slots=[inner_sec]),  # ...but nested!
    )
    plain = SimpleNamespace(
        kind=K_DEPENDS,
        target_type=None,
        dep_is_gen=False,
        dep_is_async_gen=False,
        use_cache=False,
        dep_callable=lambda: None,
        sub_plan=None,
    )
    resolver = DependencyResolver()
    # The first slot fails the safety check, so the parallelisable run
    # cannot be expanded at all — `end == start`, which the caller
    # treats as "fall back to sequential" (it only gathers when
    # `end > start + 1`).
    end = resolver._parallel_dep_group_end([outer_with_nested_sec, plain], 0)
    assert end == 0
    # The transitive-safe helper directly returns False, too.
    assert resolver._slot_safe_for_parallel(outer_with_nested_sec, set()) is False
    assert resolver._slot_safe_for_parallel(plain, set()) is True


# ── Precomputed parallel grouping (registration-time) ──────────────────

from veloce._handler_plan import compute_parallel_groups  # noqa: E402


def test_independent_deps_are_grouped():
    def a():
        return 1

    def b():
        return 2

    async def h(x: int = Depends(a), y: int = Depends(b)):
        return x + y

    plan = build_plan(h)
    # Two independent plain deps form one parallel group [0, 2).
    assert plan.parallel_groups == {0: 2}
    assert plan.parallel_groups == compute_parallel_groups(plan.slots)


def test_three_independent_deps_grouped():
    def a():
        return 1

    def b():
        return 2

    def c():
        return 3

    async def h(x: int = Depends(a), y: int = Depends(b), z: int = Depends(c)):
        return x + y + z

    assert build_plan(h).parallel_groups == {0: 3}


def test_security_dep_breaks_group():
    def a():
        return 1

    def guard():
        return "ok"

    async def h(x: int = Depends(a), s: str = Security(guard, scopes=["read"])):
        return x

    # The Security() slot is not parallel-safe, so no multi-slot group forms.
    assert build_plan(h).parallel_groups == {}


def test_yield_dep_breaks_group():
    def a():
        return 1

    def res():
        yield "r"

    async def h(x: int = Depends(a), r: str = Depends(res)):
        return x

    assert build_plan(h).parallel_groups == {}


def test_cache_collision_breaks_group():
    def a():
        return 1

    async def h(x: int = Depends(a), y: int = Depends(a)):
        return x + y

    # Same use_cache=True callable cannot share a parallel run.
    assert build_plan(h).parallel_groups == {}
