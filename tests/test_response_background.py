"""Response(background=...) plumbing tests (B2)."""

from __future__ import annotations

import asyncio

import pytest

from veloce import BackgroundTask, BackgroundTasks, Request, Response, Veloce


def _req(path: str = "/x") -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── Single BackgroundTask attached to Response ───────────────────────


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
    # Give the fire-and-forget task a chance to land.
    await asyncio.sleep(0.05)
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
    await asyncio.sleep(0.05)
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
    await asyncio.sleep(0.05)
    assert log == ["async-fired"]


@pytest.mark.asyncio
async def test_background_task_exception_does_not_break_response():
    """A failing background task is logged but never breaks the response."""

    def boom() -> None:
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
    # Let the loop pick up the rejected task; it logs but doesn't crash.
    await asyncio.sleep(0.05)


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
    await asyncio.sleep(0.05)
    assert set(log) == {"from-di", "from-response"}


def test_response_default_background_is_none():
    """Existing Response() construction without background= leaves it None."""
    r = Response(body=b"ok", content_type="text/plain")
    assert r.background is None
