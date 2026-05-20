"""@app.before_first_request — one-time init hook (the shape)."""

from __future__ import annotations

import asyncio

import pytest

from veloce import Request, Veloce


def _req(path: str = "/x") -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


@pytest.mark.asyncio
async def test_fires_on_first_request_only():
    app = Veloce(debug=True, openapi_url=None)
    fired: list[int] = []

    @app.before_first_request
    def init():
        fired.append(1)

    @app.get("/x")
    async def x():
        return {}

    await app.handle_request(_req())
    await app.handle_request(_req())
    await app.handle_request(_req())
    assert fired == [1]


@pytest.mark.asyncio
async def test_runs_in_registration_order():
    app = Veloce(debug=True, openapi_url=None)
    order: list[str] = []

    @app.before_first_request
    def a():
        order.append("a")

    @app.before_first_request
    def b():
        order.append("b")

    @app.get("/x")
    async def x():
        return {}

    await app.handle_request(_req())
    assert order == ["a", "b"]


@pytest.mark.asyncio
async def test_async_hook_supported():
    app = Veloce(debug=True, openapi_url=None)
    fired: list[int] = []

    @app.before_first_request
    async def init():
        await asyncio.sleep(0)  # genuinely async
        fired.append(1)

    @app.get("/x")
    async def x():
        return {}

    await app.handle_request(_req())
    assert fired == [1]


@pytest.mark.asyncio
async def test_concurrent_first_requests_dont_double_fire():
    """Two requests in flight at once shouldn't both run the init hooks."""
    app = Veloce(debug=True, openapi_url=None)
    fired: list[int] = []
    delay_done = asyncio.Event()

    @app.before_first_request
    async def init():
        # Hold the hook open long enough for the second request to enter
        # `handle_request` and check the flag.
        await delay_done.wait()
        fired.append(1)

    @app.get("/x")
    async def x():
        return {}

    t1 = asyncio.create_task(app.handle_request(_req()))
    t2 = asyncio.create_task(app.handle_request(_req()))
    # Yield so both tasks reach the lock-guarded section.
    await asyncio.sleep(0.01)
    delay_done.set()
    await asyncio.gather(t1, t2)
    assert fired == [1]
