"""Lifespan, request context, streaming, and response background tasks.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import asyncio

import orjson

from tests._mcp import Pipe
from tests._mcp_shared import (
    _call,
    _server,
)
from veloce import (
    Depends,
    EventSourceResponse,
    JSONResponse,
    MCPContext,
    Response,
    ServerSentEvent,
    StreamingResponse,
    Veloce,
    current_app,
    g,
)
from veloce.background import BackgroundTask
from veloce.contrib.mcp import _tasks as mcp_tasks
from veloce.contrib.mcp.registry import build_registry

# -- Lifespan, request context, streaming, response background --------


def test_mount_mcp_enters_lifespan_before_serving():
    """The serve loop runs inside `lifespan_context()`, so an `on_startup` hook
    that populates `app.state` has run before the first tool call reads it."""
    app = Veloce(openapi_url=None)

    @app.on_startup
    async def _seed() -> None:
        app.state.greeting = "ready"

    @app.mcp_tool(description="Read a value seeded at startup")
    async def read_seed() -> str:
        # Fails (AttributeError) if startup never ran before the call.
        return app.state.greeting

    async def _drive() -> dict:
        # Mirror `mount_mcp`: drive the server inside the app lifespan in-process.
        async with app.lifespan_context():
            pipe = Pipe(_server(app))
            pipe.feed(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "read_seed", "arguments": {}},
                }
            )
            return (await pipe.run())[0]

    out = asyncio.run(_drive())
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "ready"


def test_exposed_route_runs_before_request_and_uses_g_and_current_app():
    """An exposed route reading `g` / `current_app` works over MCP, and an
    `@app.before_request` hook populating `g` runs before the handler."""

    app = Veloce(openapi_url=None)

    @app.before_request
    async def _populate(request):
        g.user = "ada"

    @app.get("/whoami", expose_as_mcp_tool=True, mcp_description="Current user")
    async def whoami() -> dict:
        # Both `g` (set by before_request) and `current_app` must be bound.
        return {"user": g.user, "app": current_app.title}

    out = _call(app, "whoami", {})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload["user"] == "ada"
    assert payload["app"] == app.title


def test_before_request_short_circuit_becomes_iserror_result():
    """A `before_request` hook returning a 401 short-circuits the tool: the
    handler is not called and the denial surfaces as an isError result."""
    app = Veloce(openapi_url=None)
    called: list[str] = []

    @app.before_request
    async def _auth(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    @app.get("/secret", expose_as_mcp_tool=True, mcp_description="Protected")
    async def secret() -> dict:
        called.append("handler")
        return {"ok": True}

    out = _call(app, "secret", {})
    assert "error" not in out  # not a JSON-RPC transport error
    assert out["result"]["isError"] is True
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"detail": "unauthorized"}
    # The handler never ran - the hook short-circuited the call.
    assert called == []


def test_dependency_typed_mcpcontext_receives_context():
    """A sub-dependency declaring `ctx: MCPContext` receives the per-call
    context, not a missing-argument invalid-params error."""
    app = Veloce(openapi_url=None)

    def dep(ctx: MCPContext) -> str:
        return ctx.tool_name

    @app.mcp_tool(description="Read the tool name via a dependency")
    async def via_dep(name: str = Depends(dep)) -> str:
        return name

    # The MCPContext sub-dependency is not an agent input.
    registry = build_registry(app)
    assert "name" not in registry.tools["via_dep"].input_schema["properties"]

    out = _call(app, "via_dep", {})
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "via_dep"


def test_exposed_route_streaming_response_is_buffered():
    """A route returning a StreamingResponse is drained into a single tool result
    rather than rejected."""

    app = Veloce(openapi_url=None)

    @app.get("/stream", expose_as_mcp_tool=True, mcp_description="Stream chunks")
    async def stream() -> StreamingResponse:
        async def gen():
            yield b"hello "
            yield b"world"

        return StreamingResponse(gen(), content_type="text/plain")

    out = _call(app, "stream", {})
    assert "error" not in out
    assert out["result"].get("isError") is not True
    assert out["result"]["content"][0]["text"] == "hello world"


def test_exposed_route_streaming_json_is_decoded():
    """A streamed JSON body is buffered and decoded back to a value."""

    app = Veloce(openapi_url=None)

    @app.get("/numbers", expose_as_mcp_tool=True, mcp_description="Stream JSON")
    async def numbers() -> StreamingResponse:
        async def gen():
            yield b'{"nums":'
            yield b"[1,2,3]}"

        return StreamingResponse(gen(), content_type="application/json")

    out = _call(app, "numbers", {})
    assert "error" not in out
    assert orjson.loads(out["result"]["content"][0]["text"]) == {"nums": [1, 2, 3]}


def test_exposed_route_sse_response_is_buffered():
    """An EventSourceResponse is drained into its SSE-framed text."""

    app = Veloce(openapi_url=None)

    @app.get("/events", expose_as_mcp_tool=True, mcp_description="Stream events")
    async def events() -> EventSourceResponse:
        async def gen():
            yield ServerSentEvent(data="one")
            yield ServerSentEvent(data="two")

        return EventSourceResponse(gen())

    out = _call(app, "events", {})
    assert "error" not in out
    text = out["result"]["content"][0]["text"]
    assert "data: one" in text
    assert "data: two" in text


def test_pure_tool_streaming_response_is_buffered():
    """A pure `@app.mcp_tool` returning a StreamingResponse is buffered too."""

    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Stream chunks")
    async def stream() -> StreamingResponse:
        async def gen():
            yield b"chunk-1|"
            yield b"chunk-2"

        return StreamingResponse(gen(), content_type="text/plain")

    out = _call(app, "stream", {})
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "chunk-1|chunk-2"


def test_streaming_response_over_buffer_limit_is_in_band_error(monkeypatch):
    """A stream past the buffer limit yields an in-band error, not unbounded use."""

    monkeypatch.setattr(mcp_tasks, "_STREAM_BUFFER_LIMIT", 8)

    app = Veloce(openapi_url=None)

    @app.get("/big", expose_as_mcp_tool=True, mcp_description="Oversized stream")
    async def big() -> StreamingResponse:
        async def gen():
            yield b"0123456789ABCDEF"

        return StreamingResponse(gen(), content_type="text/plain")

    out = _call(app, "big", {})
    assert "error" not in out  # in-band tool error, not a transport error
    assert out["result"]["isError"] is True
    assert "buffer limit" in out["result"]["content"][0]["text"]


def test_handler_response_background_task_runs():
    """A handler returning `Response(background=BackgroundTask(fn))` runs fn,
    mirroring the HTTP path's response-attached background execution."""

    app = Veloce(openapi_url=None)
    ran: list[str] = []

    async def side_effect() -> None:
        ran.append("done")

    @app.get("/with-bg", expose_as_mcp_tool=True, mcp_description="Response with bg task")
    async def with_bg() -> Response:
        return Response(
            body=b"queued",
            content_type="text/plain",
            background=BackgroundTask(side_effect),
        )

    out = _call(app, "with_bg", {})
    assert "error" not in out
    # The route-derived tool unwraps the Response body, mirroring the HTTP path.
    assert out["result"]["content"][0]["text"] == "queued"
    # The response-attached background task ran after the handler returned.
    assert ran == ["done"]


def test_typed_context_still_injected():
    """A parameter typed `MCPContext` still receives the injected context even
    when named `ctx`, and is not an agent input."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Return the tool name from a typed context")
    async def named(ctx: MCPContext) -> str:
        return ctx.tool_name

    registry = build_registry(app)
    assert "ctx" not in registry.tools["named"].input_schema["properties"]

    out = _call(app, "named", {})
    assert out["result"]["content"][0]["text"] == "named"
