"""What a tool handler can ask for: dependencies, request, response, context.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import asyncio

import orjson

from tests._mcp import Pipe
from tests._mcp_shared import (
    Item,
    _call,
    _server,
)
from veloce import (
    BackgroundTasks,
    Depends,
    HTTPException,
    MCPContext,
    Request,
    Response,
    SecurityScopes,
    Veloce,
)
from veloce.contrib.mcp.registry import build_registry

# -- Dependency injection ---------------------------------------------


def test_dependency_injection_on_tool_call():
    app = Veloce(openapi_url=None)

    def get_multiplier() -> int:
        return 10

    @app.mcp_tool(description="Multiply by an injected factor")
    async def scaled(n: int, factor: int = Depends(get_multiplier)) -> int:
        return n * factor

    # `factor` is injected, so it must not appear as an agent input.
    registry = build_registry(app)
    assert "factor" not in registry.tools["scaled"].input_schema["properties"]

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "scaled", "arguments": {"n": 4}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "40"


def test_route_level_dependency_runs_on_tool_call():
    app = Veloce(openapi_url=None)
    events: list[str] = []

    def audit() -> None:
        events.append("guard")

    @app.get(
        "/report",
        expose_as_mcp_tool=True,
        mcp_description="Fetch the report",
        dependencies=[Depends(audit)],
    )
    async def report() -> dict:
        events.append("handler")
        return {"ok": True}

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "report", "arguments": {}},
        }
    )
    asyncio.run(pipe.run())
    # The route-level dependency runs before the handler, exactly as on the
    # HTTP/WS paths.
    assert events == ["guard", "handler"]


def test_route_level_dependency_can_reject_tool_call():
    app = Veloce(openapi_url=None)
    reached: list[str] = []

    def deny() -> None:
        raise HTTPException(status_code=403, detail="forbidden")

    @app.get(
        "/secret",
        expose_as_mcp_tool=True,
        mcp_description="Read the secret",
        dependencies=[Depends(deny)],
    )
    async def secret() -> dict:
        reached.append("handler")
        return {"secret": 42}

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "secret", "arguments": {}},
        }
    )
    out = asyncio.run(pipe.run())
    # A rejecting route guard aborts the call: the handler never runs and the
    # result is an in-band tool error.
    assert reached == []
    assert out[0]["result"]["isError"] is True


def test_yield_dependency_teardown_runs():
    app = Veloce(openapi_url=None)
    events: list[str] = []

    def resource():
        events.append("setup")
        yield "db"
        events.append("teardown")

    @app.mcp_tool(description="Use a yield dependency")
    async def use_res(res: str = Depends(resource)) -> str:
        events.append(f"use:{res}")
        return res

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "use_res", "arguments": {}},
        }
    )
    asyncio.run(pipe.run())
    assert events == ["setup", "use:db", "teardown"]


def test_context_is_injected():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Return the calling tool name")
    async def whoami(ctx: MCPContext) -> str:
        return ctx.tool_name

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "whoami"


def test_dependency_consumes_tool_argument():
    """A `Depends()` sub-dependency resolves a parameter named like a tool
    argument from the agent-supplied arguments, mirroring how the HTTP path
    feeds path/query params into sub-dependencies."""
    app = Veloce(openapi_url=None)

    def get_user_id(user_id: int) -> int:
        # The sub-dependency declares the same name the agent supplies; it must
        # receive the coerced argument value, not fall over looking for an HTTP
        # request attribute.
        return user_id

    @app.mcp_tool(description="Echo the resolved user id")
    async def lookup(resolved: int = Depends(get_user_id)) -> int:
        return resolved

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "lookup", "arguments": {"user_id": "42"}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "42"


def test_malformed_argument_type_is_an_in_band_tool_error():
    """A value that fails coercion is reported in band, not on the error channel.
    An argument-binding failure is a **tool execution** error reported in band
    (`result.isError`), not a JSON-RPC transport error. The spec reserves the
    error channel for an unknown tool, a malformed request or a server fault,
    and clients feed only execution errors back to the model - reporting a bad
    argument there would deny the model the one signal it can act on.

    Named for that. It used to be `..._is_invalid_params`, with a docstring and
    a leading comment both asserting the opposite of the assertion below; a
    later round added a rebuttal comment above the assertion rather than
    correcting the name and the prose, so the test read as self-contradictory.
    """
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Double an integer")
    async def double(n: int) -> int:
        return n * 2

    pipe = Pipe(_server(app))
    # `"abc"` cannot coerce to int; this is a malformed call, so it routes to
    # the JSON-RPC error channel rather than isError=true.
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "double", "arguments": {"n": "abc"}},
        }
    )
    out = asyncio.run(pipe.run())
    # Input validation is a *tool execution* error, not a protocol error: the
    # spec reserves the JSON-RPC channel for an unknown tool, a malformed
    # request, or a server fault, and clients feed only execution errors back
    # to the model. Reporting a bad argument on the error channel would deny
    # the model the one signal it can act on.
    assert out[0]["result"]["isError"] is True
    assert "n" in out[0]["result"]["content"][0]["text"]


def test_malformed_model_argument_is_an_in_band_tool_error():
    """A body model that fails validation is reported in band.
    An argument-binding failure is a **tool execution** error reported in band
    (`result.isError`), not a JSON-RPC transport error. The spec reserves the
    error channel for an unknown tool, a malformed request or a server fault,
    and clients feed only execution errors back to the model - reporting a bad
    argument there would deny the model the one signal it can act on.

    Named for that. It used to be `..._is_invalid_params`, with a docstring and
    a leading comment both asserting the opposite of the assertion below; a
    later round added a rebuttal comment above the assertion rather than
    correcting the name and the prose, so the test read as self-contradictory.
    """
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Summarise an item")
    async def summarise(item: Item) -> str:
        return f"{item.qty}x {item.name}"

    pipe = Pipe(_server(app))
    # `qty` is not an integer; model validation fails at the binding boundary.
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "summarise", "arguments": {"item": {"name": "x", "qty": "no"}}},
        }
    )
    out = asyncio.run(pipe.run())
    # Input validation is a *tool execution* error, not a protocol error: the
    # spec reserves the JSON-RPC channel for an unknown tool, a malformed
    # request, or a server fault, and clients feed only execution errors back
    # to the model. Reporting a bad argument on the error channel would deny
    # the model the one signal it can act on.
    assert out[0]["result"]["isError"] is True
    assert "qty" in out[0]["result"]["content"][0]["text"]


def test_security_scopes_param_injected():
    """A handler declaring `scopes: SecurityScopes` is callable over MCP and
    receives an empty SecurityScopes (no enclosing Security() chain)."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Report the security scopes")
    async def whatscopes(scopes: SecurityScopes) -> str:
        # An MCP call has no Security() chain, so the scope list is empty.
        assert isinstance(scopes, SecurityScopes)
        return ",".join(scopes.scopes)

    # The SecurityScopes slot is not an agent input.
    registry = build_registry(app)
    assert "scopes" not in registry.tools["whatscopes"].input_schema["properties"]

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "whatscopes", "arguments": {}},
        }
    )
    out = asyncio.run(pipe.run())
    assert "error" not in out[0]
    assert out[0]["result"]["content"][0]["text"] == ""


def test_sync_handler_offloaded():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Sync add")
    def sync_add(a: int, b: int) -> int:
        return a + b

    pipe = Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "sync_add", "arguments": {"a": 3, "b": 4}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "7"


# -- Request injection ------------------------------------------------


def test_request_slot_receives_real_request():
    """A handler declaring `request: Request` receives a real, empty Request:
    `request.headers.get(...)` returns nothing and `request.state` is usable."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Read request state and a header")
    async def probe(request: Request) -> dict:
        request.state.touched = True
        return {
            "auth": request.headers.get("authorization"),
            "touched": request.state.touched,
        }

    out = _call(app, "probe", {})
    assert "error" not in out
    payload = orjson.loads(out["result"]["content"][0]["text"])
    assert payload == {"auth": None, "touched": True}


def test_dependency_reading_request_state_works():
    """A dependency that reads/writes `request.state` works over MCP (it gets a
    real Request, not a bare MCPContext) - no AttributeError."""
    app = Veloce(openapi_url=None)

    def stamp(request: Request) -> int:
        # Would raise AttributeError if a bare MCPContext were substituted here.
        request.state.x = 7
        return request.state.x

    @app.mcp_tool(description="Use a request-reading dependency")
    async def use_stamp(value: int = Depends(stamp)) -> int:
        return value

    out = _call(app, "use_stamp", {})
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "7"


# -- Response / BackgroundTasks injection -----------------------------


def test_response_slot_injected():
    """A handler declaring `response: Response` is callable; it gets a fresh
    Response to mutate without a missing-argument TypeError."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Mutate the injected response")
    async def with_response(response: Response) -> str:
        response.status_code = 201
        return "ok"

    # The Response slot is framework-injected, never an agent input.
    registry = build_registry(app)
    assert "response" not in registry.tools["with_response"].input_schema["properties"]

    out = _call(app, "with_response", {})
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "ok"


def test_background_tasks_slot_injected_and_runs():
    """A handler declaring `tasks: BackgroundTasks` is callable and the
    scheduled task actually runs after the handler returns."""
    app = Veloce(openapi_url=None)
    ran: list[str] = []

    async def record() -> None:
        ran.append("done")

    @app.mcp_tool(description="Schedule a background task")
    async def schedule(tasks: BackgroundTasks) -> str:
        tasks.add_task(record)
        return "queued"

    # The BackgroundTasks slot is framework-injected, never an agent input.
    registry = build_registry(app)
    assert "tasks" not in registry.tools["schedule"].input_schema["properties"]

    out = _call(app, "schedule", {})
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "queued"
    # The background task ran after the handler returned, mirroring HTTP.
    assert ran == ["done"]


# -- Type-based context detection -------------------------------------


def test_plain_argument_named_context_is_an_input():
    """A tool argument named `context` but typed `str` stays a normal input -
    detection of the MCPContext is by TYPE, never by parameter name."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Echo a string named context")
    async def echo(context: str) -> str:
        return context

    # `context` is a real, required agent input - it appears in the schema.
    registry = build_registry(app)
    props = registry.tools["echo"].input_schema["properties"]
    assert "context" in props

    out = _call(app, "echo", {"context": "hi"})
    assert "error" not in out
    assert out["result"]["content"][0]["text"] == "hi"
