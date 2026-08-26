"""Response(background=...) plumbing tests (B2)."""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import make_request
from veloce import BackgroundTask, BackgroundTasks, Request, Response, Veloce


def _req(path: str = "/x") -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── Single BackgroundTask attached to Response ───────────────────────


async def _until(predicate, *, turns: int = 2000) -> None:
    """Advance the event loop until `predicate()` holds.

    Replaces `await asyncio.sleep(0.05)` after a fire-and-forget task. A fixed
    sleep is a guess in both directions: too short and the suite is flaky on a
    loaded machine, too long and every such test pays for the worst case. This
    yields to the loop and re-checks, so it returns as soon as the task has run
    and raises a named failure if it never does - rather than falling through to
    an assertion whose message is about the wrong thing.

    The same module already demonstrated the deterministic idiom with an
    `asyncio.Event`; this is the form that needs no change to the task itself.
    """

    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the background task never ran")


@pytest.mark.asyncio
async def test_response_carries_single_background_task():
    log: list[str] = []

    def cleanup(label: str) -> None:
        log.append(label)

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(
            body=b"ok",
            content_type="text/plain",
            background=BackgroundTask(cleanup, "fired"),
        )

    resp = await app.handle_request(_req())
    assert resp.status_code == 200
    await _until(lambda: log == ["fired"])
    assert log == ["fired"]


@pytest.mark.asyncio
async def test_response_carries_background_tasks_collection():
    """A `BackgroundTasks` (plural) collection also works via the same kwarg."""
    log: list[str] = []
    tasks = BackgroundTasks()
    tasks.add_task(lambda: log.append("a"))
    tasks.add_task(lambda: log.append("b"))

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(body=b"ok", content_type="text/plain", background=tasks)

    await app.handle_request(_req())
    await _until(lambda: log == ["a", "b"])
    assert log == ["a", "b"]


@pytest.mark.asyncio
async def test_response_without_background_does_not_break():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(body=b"ok", content_type="text/plain")

    resp = await app.handle_request(_req())
    assert resp.status_code == 200
    assert resp.body == b"ok"


@pytest.mark.asyncio
async def test_async_background_task():
    """Async BackgroundTask is awaited as a coroutine."""
    log: list[str] = []

    async def cleanup() -> None:
        await asyncio.sleep(0)
        log.append("async-fired")

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(
            body=b"ok",
            content_type="text/plain",
            background=BackgroundTask(cleanup),
        )

    await app.handle_request(_req())
    await _until(lambda: log == ["async-fired"])
    assert log == ["async-fired"]


@pytest.mark.asyncio
async def test_background_task_exception_does_not_break_response():
    """A failing background task is logged but never breaks the response."""

    ran: list[str] = []

    def boom() -> None:
        # Recorded before raising, so the wait below has something to observe.
        # The sleep this replaced waited for a task it could not see, and the
        # test asserted nothing about it - so it passed whether or not the task
        # ever ran.
        ran.append("boom")
        raise RuntimeError("kaboom")

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(
            body=b"ok",
            content_type="text/plain",
            background=BackgroundTask(boom),
        )

    resp = await app.handle_request(_req())
    assert resp.status_code == 200
    assert resp.body == b"ok"
    await _until(lambda: ran == ["boom"])
    assert ran == ["boom"]


@pytest.mark.asyncio
async def test_response_background_with_di_injected_tasks_coexist():
    """A handler can mix both shapes: DI-injected BackgroundTasks AND a
    Response(background=...) attachment. Both fire."""
    log: list[str] = []
    response_attached = BackgroundTask(lambda: log.append("from-response"))

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x(bg: BackgroundTasks):
        bg.add_task(lambda: log.append("from-di"))
        return Response(
            body=b"ok",
            content_type="text/plain",
            background=response_attached,
        )

    await app.handle_request(_req())
    await _until(lambda: set(log) == {"from-di", "from-response"})
    assert set(log) == {"from-di", "from-response"}


# ── Shutdown drain: background tasks are not orphaned ────────────────


@pytest.mark.asyncio
async def test_response_background_task_is_drained_on_shutdown():
    """A still-running response background task is tracked and cancelled-and-
    drained on shutdown, not orphaned. Pre-fix it was a bare `create_task`
    the shutdown drain never saw, so it could outlive the loop."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def long_bg() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        return Response(
            body=b"ok",
            content_type="text/plain",
            background=BackgroundTask(long_bg),
        )

    await app.handle_request(_req())
    await started.wait()
    # Tracked in the single spawn registry, so the shutdown drain sees it.
    assert len(app._spawned_anon) == 1
    await app._drain_spawned_tasks()
    # Cancelled and awaited rather than left pending past shutdown.
    assert cancelled.is_set()
    assert not app._spawned_anon


@pytest.mark.asyncio
async def test_di_injected_background_tasks_are_drained_on_shutdown():
    """The DI-injected `BackgroundTasks` queue goes through the same tracked
    spawn path, so its work is drained on shutdown too."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def long_bg() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x(bg: BackgroundTasks):
        bg.add_task(long_bg)
        return Response(body=b"ok", content_type="text/plain")

    await app.handle_request(_req())
    await started.wait()
    assert len(app._spawned_anon) == 1
    await app._drain_spawned_tasks()
    assert cancelled.is_set()
    assert not app._spawned_anon


def test_response_default_background_is_none():
    """Existing Response() construction without background= leaves it None."""
    r = Response(body=b"ok", content_type="text/plain")
    assert r.background is None


def test_convenience_subclasses_accept_background():
    """JSON/HTML/PlainText responses forward `background=` to the base Response,
    so a BackgroundTask can be attached to them the same way it can to Response."""
    from veloce import HTMLResponse, JSONResponse, PlainTextResponse

    task = BackgroundTask(lambda: None)
    assert JSONResponse({"ok": True}, background=task).background is task
    assert HTMLResponse("<p>x</p>", background=task).background is task
    assert PlainTextResponse("x", background=task).background is task
