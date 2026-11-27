"""Sibling `Depends()` slots resolve concurrently when safe (CL10).

When a handler declares multiple independent `Depends()` parameters,
the resolver runs them under `asyncio.gather` instead of awaiting them
one at a time. The win shows up clearly when each dependency does I/O
(an `asyncio.sleep` here stands in for any awaitable wait): the total
resolve time becomes `max(dep_durations)` rather than `sum(...)`.

Constraints — preserved by `compute_dep_waves`:
- Security() scope-pushing dependencies stay sequential.
- yield-style dependencies stay sequential.
- two siblings sharing a `use_cache=True` callable stay sequential.
"""

from __future__ import annotations

from tests._dep_rendezvous import rendezvous_pair
from veloce import Depends, Security, Veloce
from veloce._handler_plan import (
    _slot_parallel_safe,
    build_plan,
    compute_dep_waves,
)
from veloce.testclient import TestClient


def test_independent_async_siblings_run_in_parallel():
    """Two sibling Depends() begin concurrently rather than sequentially.

    **Proven structurally, not by a clock.** This compared two `time.monotonic()`
    samples against a 25 ms budget after two real 50 ms sleeps - a wall-clock
    threshold in the default suite, which is the class of test this project
    excludes behind the `perf` marker, and which fails under CI contention for
    reasons unrelated to the code. The previous docstring argued the start-delta
    was the *robust* measure of the two available; it is still a clock.

    The dependencies now meet at a rendezvous, so concurrency is proven by the
    request succeeding at all: sequential resolution cannot get past it.
    """
    app = Veloce(openapi_url=None)
    slow_a, slow_b, arrived, both_here = rendezvous_pair()

    @app.get("/parallel")
    async def handler(a: str = Depends(slow_a), b: str = Depends(slow_b)) -> dict:
        return {"a": a, "b": b}

    resp = TestClient(app).get("/parallel")
    assert resp.status_code == 200
    assert resp.json() == {"a": "a", "b": "b"}
    assert sorted(arrived) == ["a", "b"]
    assert both_here.is_set()


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


def test_the_wave_builder_excludes_a_security_dependency():
    """A Security() slot is left out of the batch, not batched with its siblings.

    Driven through `build_plan` rather than `SimpleNamespace` stubs. The stubs
    spelled out seven `_Slot` fields by hand, so a field added to the real slot -
    or renamed - would leave this test asserting about a shape the wave builder
    no longer receives, and `_slot_parallel_safe` reading a default where a real
    slot carries a value.
    """

    def a():
        return 1

    def b():
        return 2

    def guard():
        return "ok"

    async def h(
        p1: int = Depends(a),
        p2: int = Depends(b),
        s: str = Security(guard, scopes=["read"]),
        p3: int = Depends(a),
    ):
        return p1

    # The two plain slots before the Security() one batch together; the
    # Security() slot is excluded entirely so its scope push/pop stays ordered,
    # and the plain slot after it cannot join a batch that has already closed.
    assert compute_dep_waves(build_plan(h).slots) == [[0, 1], [3]]


def test_group_end_helper_refuses_nested_security():
    """A plain `Depends` whose sub-plan transitively contains a Security() slot
    must not be parallelised - the shared `_scope_stack` would otherwise be
    corrupted by interleaved push/pop pairs across sibling tasks.

    Also built from a real plan: the nesting is what makes this case
    interesting, and a hand-built `sub_plan` is the part a stub gets wrong.
    """

    def inner_guard():
        return "ok"

    def outer(s: str = Security(inner_guard, scopes=["s1"])):
        return s

    def plain():
        return 1

    async def h(a: str = Depends(outer), b: int = Depends(plain)):
        return a

    outer_slot = build_plan(h).slots[0]
    assert not _slot_parallel_safe(outer_slot, set()), (
        "an outer plain Depends reaching a Security() through its sub-plan was "
        "treated as parallel-safe"
    )


# ── Precomputed dependency waves (registration-time) ───────────────────


def test_independent_deps_are_grouped():
    def a():
        return 1

    def b():
        return 2

    async def h(x: int = Depends(a), y: int = Depends(b)):
        return x + y

    plan = build_plan(h)
    # Two independent plain deps form one wave.
    assert compute_dep_waves(plan.slots) == [[0, 1]]
    # Twice, because the waves are derived from the plan rather than cached
    # on it: a second call must give the same answer, not a consumed one.
    assert compute_dep_waves(plan.slots) == [[0, 1]]


def test_three_independent_deps_grouped():
    def a():
        return 1

    def b():
        return 2

    def c():
        return 3

    async def h(x: int = Depends(a), y: int = Depends(b), z: int = Depends(c)):
        return x + y + z

    assert compute_dep_waves(build_plan(h).slots) == [[0, 1, 2]]


def test_security_dep_breaks_group():
    def a():
        return 1

    def guard():
        return "ok"

    async def h(x: int = Depends(a), s: str = Security(guard, scopes=["read"])):
        return x

    # The Security() slot is not parallel-safe, so one safe dep is left and
    # `compute_dep_waves` builds nothing.
    assert compute_dep_waves(build_plan(h).slots) == []


def test_yield_dep_breaks_group():
    def a():
        return 1

    def res():
        yield "r"

    async def h(x: int = Depends(a), r: str = Depends(res)):
        return x

    assert compute_dep_waves(build_plan(h).slots) == []


def test_cache_collision_breaks_group():
    def a():
        return 1

    async def h(x: int = Depends(a), y: int = Depends(a)):
        return x + y

    # Same use_cache=True callable cannot share a wave.
    assert compute_dep_waves(build_plan(h).slots) == []
