"""Straight-line dispatch fast path — engagement, eligibility, and behavior parity.

The fast path collapses the dispatch orchestration when the app has no active
feature (`cp.is_bare`) and the matched route is `is_fast_eligible`. These tests
assert it engages where expected, disengages where it must, and stays behavior-
identical to the full path for the cases the bench prototype never exercised:
one-shot `after_this_request` callbacks, response-attached background tasks,
custom exception handlers, generic 500s, and HEAD body stripping.
"""

from __future__ import annotations

import asyncio

from tests.conftest import make_request
from veloce import (
    BackgroundTask,
    HTTPException,
    JSONResponse,
    Request,
    Response,
    Veloce,
    after_this_request,
)


def _req(method: str = "GET", path: str = "/x") -> Request:
    return make_request(method=method, path=path, query_string="", headers={}, body=b"")


def test_fast_path_engages_for_bare_app():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return {"ok": True}

    assert app._ensure_pipeline().is_bare is True
    match = app.match("GET", "/x")
    assert match is not None
    assert match.route_info.is_fast_eligible is True


async def test_fast_path_returns_correct_response():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(request):
        return {"hello": "world"}

    response = await app.handle_request(_req(path="/x"))
    assert response.status_code == 200
    assert b'"hello"' in response.body


async def test_after_this_request_runs_on_fast_path():
    app = Veloce(openapi_url=None)
    fired: list[int] = []

    @app.get("/x")
    async def x():
        @after_this_request
        def cb(request, response):
            fired.append(1)

        return {}

    assert app._ensure_pipeline().is_bare is True
    await app.handle_request(_req(path="/x"))
    assert fired == [1]


async def test_response_background_task_runs_on_fast_path():
    app = Veloce(openapi_url=None)
    ran: list[int] = []

    @app.get("/x")
    async def x():
        return Response(body=b"ok", background=BackgroundTask(lambda: ran.append(1)))

    assert app._ensure_pipeline().is_bare is True
    await app.handle_request(_req(path="/x"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert ran == [1]


async def test_http_exception_uses_custom_handler_on_fast_path():
    app = Veloce(openapi_url=None)

    @app.exception_handler(404)
    async def not_found(request, exc):
        return JSONResponse({"custom": True}, status_code=404)

    @app.get("/x")
    async def x():
        raise HTTPException(status_code=404)

    # The exception handler does not arm any pipeline feature, so the route
    # still takes the fast path; the raise must route through the shared ladder.
    assert app._ensure_pipeline().is_bare is True
    response = await app.handle_request(_req(path="/x"))
    assert response.status_code == 404
    assert b'"custom"' in response.body


async def test_generic_exception_returns_500_on_fast_path():
    app = Veloce(openapi_url=None)  # debug off -> 500, not propagated

    @app.get("/x")
    async def x():
        raise ValueError("boom")

    assert app._ensure_pipeline().is_bare is True
    response = await app.handle_request(_req(path="/x"))
    assert response.status_code == 500


async def test_head_empty_body_with_content_length_on_fast_route():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x():
        return {"hello": "world"}

    received: dict = {"chunks": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            received["status"] = msg["status"]
            received["headers"] = dict(msg["headers"])
        elif msg["type"] == "http.response.body":
            received["chunks"].append(msg.get("body", b""))

    await app(
        {"type": "http", "method": "HEAD", "path": "/x", "query_string": b"", "headers": []},
        receive,
        send,
    )
    assert received["status"] == 200
    assert b"".join(received["chunks"]) == b""
    assert received["headers"][b"content-length"] == b"17"  # len('{"hello":"world"}')


async def test_before_request_hook_disables_fast_path_and_runs():
    app = Veloce(openapi_url=None)
    order: list[str] = []

    @app.before_request
    def hook(request):
        order.append("hook")

    @app.get("/x")
    async def x():
        order.append("handler")
        return {}

    # A before_request hook bumps the generation so the recompiled pipeline is
    # no longer bare; the fast path must disengage and the hook must run.
    assert app._ensure_pipeline().is_bare is False
    await app.handle_request(_req(path="/x"))
    assert order == ["hook", "handler"]


def test_response_model_route_not_fast_eligible():
    from pydantic import BaseModel

    class Out(BaseModel):
        x: int

    app = Veloce(openapi_url=None)

    @app.get("/x", response_model=Out)
    async def x():
        return {"x": 1, "secret": "hidden"}

    assert app.match("GET", "/x").route_info.is_fast_eligible is False


def test_non_default_status_not_fast_eligible():
    app = Veloce(openapi_url=None)

    @app.get("/x", status_code=201)
    async def x():
        return {}

    assert app.match("GET", "/x").route_info.is_fast_eligible is False


def test_sync_handler_not_fast_eligible():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    def x():  # sync handler -> offloaded to executor, not fast-eligible
        return {}

    assert app.match("GET", "/x").route_info.is_fast_eligible is False
