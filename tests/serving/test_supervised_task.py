"""Supervised self-restarting background tasks via `app.supervise(...)`.

A supervised coroutine is restarted on failure with bounded backoff and a
count-within-window circuit breaker, logs each crash, and is cancelled and
drained on shutdown like any other spawned task.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce import Veloce


async def test_supervise_restarts_after_crash():
    runs: list[int] = []
    third_run = asyncio.Event()

    async def worker():
        runs.append(1)
        if len(runs) < 3:
            raise RuntimeError("boom")
        third_run.set()
        # Third run blocks until cancelled at shutdown.
        await asyncio.Event().wait()

    app = Veloce()
    await app._run_lifecycle("startup")
    app.supervise(lambda: worker(), name="w", backoff=0.0, max_restarts=10)

    # Wait on the worker's own signal. A fixed budget of `sleep(0)` ticks
    # encodes how many awaits the supervisor currently takes between
    # restarts, so one extra `await` in it turns this into a restart-is-
    # broken failure that is really a stale budget.
    await asyncio.wait_for(third_run.wait(), timeout=1.0)

    assert len(runs) == 3
    await app._run_lifecycle("shutdown")


async def test_supervise_circuit_breaker_gives_up():
    runs: list[int] = []

    async def always_crash():
        runs.append(1)
        raise RuntimeError("always")

    app = Veloce()
    await app._run_lifecycle("startup")
    # Tight crash loop, large window so failures all count: should give up at 3.
    app.supervise(
        lambda: always_crash(),
        name="cb",
        backoff=0.0,
        max_restarts=3,
        restart_window=1000.0,
    )

    task = app.get_spawned_task("cb")
    assert task is not None
    # The supervisor returns (does not raise) once the breaker trips.
    await asyncio.wait([task], timeout=1.0)

    assert task.done()
    assert not task.cancelled()
    # The initial run plus max_restarts restarts = 4 runs, then give up.
    assert len(runs) == 4
    await app._run_lifecycle("shutdown")


async def test_supervise_cancelled_on_shutdown():
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.Event().wait()

    app = Veloce()
    await app._run_lifecycle("startup")
    task = app.supervise(lambda: forever(), name="f")

    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert not task.done()

    await app._run_lifecycle("shutdown")
    assert task.done()
    assert app.get_spawned_task("f") is None


async def test_supervise_requires_callable_factory():
    app = Veloce()
    await app._run_lifecycle("startup")

    async def worker():
        return None

    coro = worker()
    with pytest.raises(TypeError, match="zero-argument callable"):
        app.supervise(coro, name="bad")  # type: ignore[arg-type]
    coro.close()
    await app._run_lifecycle("shutdown")


async def test_supervise_duplicate_name_raises():
    app = Veloce()
    await app._run_lifecycle("startup")

    async def idle():
        await asyncio.Event().wait()

    app.supervise(lambda: idle(), name="dup")
    with pytest.raises(ValueError, match="dup"):
        app.supervise(lambda: idle(), name="dup")
    await app._run_lifecycle("shutdown")


async def test_supervise_factory_returning_non_awaitable_fails_fast():
    """A factory returning a non-awaitable (programmer error) raises immediately
    rather than being treated as a crash and retried to the breaker."""
    app = Veloce()
    with pytest.raises(TypeError, match="awaitable"):
        await app._supervise_loop(
            lambda: None,  # not a coroutine
            name="bad",
            max_restarts=5,
            restart_window=1.0,
            backoff=0.0,
            max_backoff=0.0,
        )


async def test_supervise_max_restarts_one_allows_one_restart():
    """`max_restarts=1` allows exactly one restart (initial run + 1 = 2 runs),
    not zero - the breaker counts restarts, not failed runs."""
    runs: list[int] = []

    async def crash():
        runs.append(1)
        raise RuntimeError("x")

    app = Veloce()
    await app._run_lifecycle("startup")
    app.supervise(lambda: crash(), name="one", backoff=0.0, max_restarts=1, restart_window=1000.0)
    task = app.get_spawned_task("one")
    assert task is not None
    await asyncio.wait([task], timeout=1.0)
    assert task.done() and not task.cancelled()
    assert len(runs) == 2
    await app._run_lifecycle("shutdown")


async def test_supervise_factory_sync_exception_restarts():
    """A factory that raises during synchronous setup is a crash (restart), not
    a fatal error that stops supervision."""
    runs: list[int] = []

    third_call = asyncio.Event()

    def factory():
        runs.append(len(runs) + 1)
        if len(runs) < 3:
            raise RuntimeError("setup failed")
        third_call.set()

        async def ok():
            await asyncio.Event().wait()  # long-lived success

        return ok()

    app = Veloce()
    await app._run_lifecycle("startup")
    app.supervise(factory, name="f", backoff=0.0, max_restarts=10, restart_window=1000.0)
    task = app.get_spawned_task("f")
    assert task is not None
    await asyncio.wait_for(third_call.wait(), timeout=1.0)
    # Factory raised twice (each a crash + restart), then returned a coroutine.
    assert len(runs) == 3
    assert not task.done()
    await app._run_lifecycle("shutdown")
