"""Waiting for a response's background tasks, without driving the loop by hand.

A background task runs *after* the response is returned - that is the point of
it - so a test asserting its effect has nothing to await. The suite reached for
`client._loop.run_until_complete(_drain())` with a hand-picked number of turns:
private, and a guess in both directions. Five turns is too many when the task
finished immediately and too few when it awaited anything.

`app.wait_for_background_tasks()` waits on the tasks themselves. It waits; it
does not cancel - `_drain_spawned_tasks` is the shutdown path that does - and it
re-reads the set each round, so a task that spawns another is waited for in
full.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import make_request
from veloce import BackgroundTasks, Request, Veloce
from veloce.testclient import AsyncTestClient, TestClient


def _app(work) -> Veloce:
    app = Veloce(openapi_url=None)

    @app.post("/go")
    async def go(request: Request, tasks: BackgroundTasks):
        tasks.add_task(work)
        return {"queued": True}

    return app


# ── it actually waits ────────────────────────────────────────────────


async def test_it_waits_for_a_task_that_has_not_finished():
    """The claim. A task that awaits something is not done when the response is."""
    done = []

    async def work():
        await asyncio.sleep(0.01)
        done.append("work")

    app = _app(work)
    await app.handle_request(make_request(method="POST", path="/go"))
    assert done == []
    assert await app.wait_for_background_tasks() is True
    assert done == ["work"]


async def test_it_returns_true_when_everything_finished():
    async def work():
        await asyncio.sleep(0)

    app = _app(work)
    await app.handle_request(make_request(method="POST", path="/go"))
    assert await app.wait_for_background_tasks() is True


async def test_it_returns_immediately_when_there_is_nothing_to_wait_for():
    assert await Veloce(openapi_url=None).wait_for_background_tasks() is True


async def test_it_waits_for_several_tasks():
    done = []

    app = Veloce(openapi_url=None)

    @app.post("/go")
    async def go(request: Request, tasks: BackgroundTasks):
        for n in range(3):

            async def work(n=n):
                await asyncio.sleep(0.01)
                done.append(n)

            tasks.add_task(work)
        return {"queued": True}

    await app.handle_request(make_request(method="POST", path="/go"))
    await app.wait_for_background_tasks()
    assert sorted(done) == [0, 1, 2]


async def test_a_task_that_spawns_another_is_waited_for_in_full():
    """The set is re-read each round, so the second task is not missed."""
    done = []

    app = Veloce(openapi_url=None)

    async def second():
        await asyncio.sleep(0.01)
        done.append("second")

    async def first():
        await asyncio.sleep(0.01)
        done.append("first")
        app.spawn(second())

    @app.post("/go")
    async def go(request: Request, tasks: BackgroundTasks):
        tasks.add_task(first)
        return {"queued": True}

    await app.handle_request(make_request(method="POST", path="/go"))
    assert await app.wait_for_background_tasks() is True
    assert done == ["first", "second"]


# ── the timeout ──────────────────────────────────────────────────────


async def test_it_reports_false_on_timeout():
    """A slow task must not hang the caller forever."""
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.sleep(30)

    app = _app(work)
    await app.handle_request(make_request(method="POST", path="/go"))
    await started.wait()
    assert await app.wait_for_background_tasks(timeout=0.05) is False


async def test_a_timeout_does_not_cancel_the_task():
    """It waits; it does not cancel. Cancelling would make a timeout
    destructive, and the shutdown drain is what cancels."""
    started = asyncio.Event()
    cancelled = []

    async def work():
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append("yes")
            raise

    app = _app(work)
    await app.handle_request(make_request(method="POST", path="/go"))
    await started.wait()
    await app.wait_for_background_tasks(timeout=0.05)
    assert cancelled == []
    await app._drain_spawned_tasks()


# ── through the test clients ─────────────────────────────────────────


def test_the_sync_client_waits():
    done = []

    async def work():
        await asyncio.sleep(0.01)
        done.append("work")

    with TestClient(_app(work)) as client:
        client.post("/go")
        assert client.wait_for_background_tasks() is True
    assert done == ["work"]


def test_the_sync_client_reports_a_timeout():
    async def work():
        await asyncio.sleep(30)

    with TestClient(_app(work)) as client:
        client.post("/go")
        assert client.wait_for_background_tasks(timeout=0.05) is False


async def test_the_async_client_waits():
    done = []

    async def work():
        await asyncio.sleep(0.01)
        done.append("work")

    async with AsyncTestClient(_app(work)) as client:
        await client.post("/go")
        assert await client.wait_for_background_tasks() is True
    assert done == ["work"]


def test_waiting_with_no_request_made_is_a_no_op():
    """The negative: it must not block when nothing was ever scheduled."""

    async def work():
        pass

    with TestClient(_app(work)) as client:
        assert client.wait_for_background_tasks() is True


# ── a failing task does not become the caller's exception ────────────


async def test_a_failing_task_still_completes_the_wait():
    """Failures are logged through the spawn path; the wait reports completion,
    it does not re-raise. Otherwise every caller would need a try/except for
    work it did not write."""

    async def work():
        raise RuntimeError("boom")

    app = _app(work)
    await app.handle_request(make_request(method="POST", path="/go"))
    assert await app.wait_for_background_tasks() is True


@pytest.mark.parametrize("timeout", [None, 0.5, 5.0])
async def test_the_timeout_argument_is_accepted_in_every_form(timeout):
    async def work():
        await asyncio.sleep(0)

    app = _app(work)
    await app.handle_request(make_request(method="POST", path="/go"))
    assert await app.wait_for_background_tasks(timeout) is True
