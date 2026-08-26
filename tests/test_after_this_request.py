"""after_this_request — one-shot after-request hook."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce, after_this_request


def _req(path: str = "/x") -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


def test_outside_request_raises():
    with pytest.raises(RuntimeError, match="active request context"):
        after_this_request(lambda req, resp: None)


@pytest.mark.asyncio
async def test_one_shot_callback_runs_for_current_request_only():
    app = Veloce(debug=True, openapi_url=None)
    fired: list[int] = []

    @app.get("/x")
    async def x():
        @after_this_request
        def cb(request, response):
            fired.append(1)

        return {}

    @app.get("/y")
    async def y():
        return {}

    await app.handle_request(_req("/x"))
    await app.handle_request(_req("/y"))
    # Fired exactly once — the second request shouldn't trigger the
    # callback the first request registered.
    assert fired == [1]


@pytest.mark.asyncio
async def test_callback_can_mutate_response():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x")
    async def x():
        @after_this_request
        def cb(request, response):
            response.headers["X-Late"] = "yes"

        return {"ok": True}

    resp = await app.handle_request(_req("/x"))
    assert resp.headers["X-Late"] == "yes"


@pytest.mark.asyncio
async def test_callback_runs_after_global_after_request():
    app = Veloce(debug=True, openapi_url=None)
    order: list[str] = []

    @app.after_request
    def global_hook(request, response):
        order.append("global")

    @app.get("/x")
    async def x():
        @after_this_request
        def cb(request, response):
            order.append("one_shot")

        return {}

    await app.handle_request(_req("/x"))
    assert order == ["global", "one_shot"]


@pytest.mark.asyncio
async def test_multiple_callbacks_drain_in_registration_order():
    app = Veloce(debug=True, openapi_url=None)
    order: list[int] = []

    @app.get("/x")
    async def x():
        @after_this_request
        def first(request, response):
            order.append(1)

        @after_this_request
        def second(request, response):
            order.append(2)

        return {}

    await app.handle_request(_req("/x"))
    assert order == [1, 2]
