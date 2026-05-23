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
    """Two sibling Depends() that each sleep for ~50 ms should land in ~50 ms total."""
    app = Veloce(openapi_url=None)

    async def slow_a() -> str:
        await asyncio.sleep(0.05)
        return "a"

    async def slow_b() -> str:
        await asyncio.sleep(0.05)
        return "b"

    @app.get("/parallel")
    async def handler(a: str = Depends(slow_a), b: str = Depends(slow_b)) -> dict:
        return {"a": a, "b": b}

    t0 = time.perf_counter()
    resp = TestClient(app).get("/parallel")
    elapsed = time.perf_counter() - t0

    assert resp.status_code == 200
    assert resp.json() == {"a": "a", "b": "b"}
    # Sequential would be ~100 ms; parallel ~50 ms. Allow generous
    # headroom for CI; the win is ~2x even with overhead.
    assert elapsed < 0.085, f"expected parallel resolve, got {elapsed:.3f}s"


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
    )
    sec = SimpleNamespace(
        kind=K_DEPENDS,
        target_type=["read"],  # scope list — Security() shape
        dep_is_gen=False,
        dep_is_async_gen=False,
        use_cache=False,
        dep_callable=lambda: None,
    )
    resolver = DependencyResolver()
    # plain, plain, sec, plain — the run should stop at the Security() slot.
    end = resolver._parallel_dep_group_end([plain, plain, sec, plain], 0)
    assert end == 2
