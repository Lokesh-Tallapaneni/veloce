"""BackgroundTasks dependency injection into handlers."""

from __future__ import annotations

import asyncio

from tests.conftest import make_request
from veloce import BackgroundTasks, Request, Veloce


async def _until(predicate, turns: int = 500) -> None:
    """Yield to the loop until `predicate` holds.

    The background task is scheduled with `create_task`, so what has to elapse
    is loop turns, not wall-clock time. This module used `sleep(0.05)`, which is
    both slower than needed and a guess that a loaded machine can lose.

    The later turns yield real time. A *sync* background callable is offloaded to
    a thread-pool worker, and `sleep(0)` yields to the event loop without
    consuming any wall-clock - so a pure-yield spin can burn its whole budget in
    microseconds while the worker has not been scheduled once. That is a flake
    that appears only under load; the first turns stay zero-cost for work that is
    already on the loop.
    """
    for turn in range(turns):
        if predicate():
            return
        await asyncio.sleep(0 if turn < 10 else 0.001)
    raise AssertionError("the background task never ran")


class TestBackgroundTasksInjection:
    async def test_background_tasks_injected(self):
        app = Veloce(openapi_url=None)
        results = []

        async def bg_work(val: str):
            results.append(val)

        @app.post("/work")
        async def do_work(request: Request, tasks: BackgroundTasks):
            tasks.add_task(bg_work, "done")
            return {"status": "queued"}

        resp = await app.handle_request(make_request(method="POST", path="/work"))
        assert resp.status_code == 200
        await _until(lambda: results)
        assert results == ["done"]

    async def test_the_response_is_returned_before_the_task_runs(self):
        """The point of a background task: the client is not kept waiting."""
        app = Veloce(openapi_url=None)
        gate = asyncio.Event()
        ran = []

        async def bg_work():
            await gate.wait()
            ran.append("done")

        @app.post("/work")
        async def do_work(request: Request, tasks: BackgroundTasks):
            tasks.add_task(bg_work)
            return {"status": "queued"}

        resp = await app.handle_request(make_request(method="POST", path="/work"))
        assert resp.status_code == 200
        assert ran == []
        gate.set()
        await _until(lambda: ran)

    async def test_several_tasks_all_run(self):
        app = Veloce(openapi_url=None)
        results = []

        async def bg_work(val: int):
            results.append(val)

        @app.post("/work")
        async def do_work(request: Request, tasks: BackgroundTasks):
            for n in range(3):
                tasks.add_task(bg_work, n)
            return {"status": "queued"}

        await app.handle_request(make_request(method="POST", path="/work"))
        await _until(lambda: len(results) == 3)
        assert sorted(results) == [0, 1, 2]

    async def test_a_sync_task_runs_too(self):
        app = Veloce(openapi_url=None)
        results = []

        @app.post("/work")
        async def do_work(request: Request, tasks: BackgroundTasks):
            tasks.add_task(results.append, "sync")
            return {"status": "queued"}

        await app.handle_request(make_request(method="POST", path="/work"))
        await _until(lambda: results)
        assert results == ["sync"]
