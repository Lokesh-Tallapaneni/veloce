"""MCP dispatch benchmarks — the agent-facing message path.

The HTTP hot path has had coverage since the suite was written; the MCP path had
none, which left every change to it argued rather than measured. These cover the
two messages an agent actually spends its time in: `tools/call`, which replays the
route lifecycle, and `tools/list`, which an agent issues at session start and
whose cost scales with the number of registered tools.

A configured visibility policy crosses a thread, which instruction counting cannot
see; that case lives in `benchmarks/walltime/` instead.

Each server is built once at import and warmed with a single message so registry
assembly, plan compilation and the first-call latches are out of the measurement.
"""

from __future__ import annotations

from pydantic import BaseModel

from benchmarks.conftest import run_async
from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer

TOOL_COUNT = 50


class Item(BaseModel):
    name: str
    price: float
    tags: list[str] = []


def _warm(server: MCPServer, message: dict) -> MCPServer:
    """Drive one message so first-call setup is out of the measurement."""
    run_async(server._tools_call(message))
    run_async(server._handle_tools_list({}))
    return server


# ── Pure tool call ─────────────────────────────────────────


def _build_pure_tool_server() -> MCPServer:
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> dict:
        return {"sum": a + b}

    return _warm(MCPServer(app), {"name": "add", "arguments": {"a": 2, "b": 3}})


PURE_TOOL_SERVER = _build_pure_tool_server()


def test_mcp_tools_call_pure(benchmark):
    """A `@app.mcp_tool` with no route: bind arguments, call, shape the result."""
    result = benchmark(
        lambda: run_async(
            PURE_TOOL_SERVER._tools_call({"name": "add", "arguments": {"a": 2, "b": 3}})
        )
    )
    assert result["content"]


# ── Pure tool with a validated model argument ──────────────


def _build_model_tool_server() -> MCPServer:
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Create an item")
    async def create(item: Item) -> dict:
        return {"name": item.name, "price": item.price}

    payload = {"name": "widget", "price": 9.99, "tags": ["a", "b"]}
    return _warm(MCPServer(app), {"name": "create", "arguments": {"item": payload}})


MODEL_TOOL_SERVER = _build_model_tool_server()


def test_mcp_tools_call_model_argument(benchmark):
    """Validate a Pydantic model argument through the tool boundary."""
    message = {
        "name": "create",
        "arguments": {"item": {"name": "widget", "price": 9.99, "tags": ["a", "b"]}},
    }
    result = benchmark(lambda: run_async(MODEL_TOOL_SERVER._tools_call(message)))
    assert result["content"]


# ── Route-backed tool call ─────────────────────────────────


def _build_route_tool_server() -> MCPServer:
    app = Veloce(openapi_url=None)

    @app.get(
        "/items/{item_id:int}",
        expose_as_mcp_tool=True,
        mcp_description="Fetch an item by id",
    )
    async def read_item(item_id: int, verbose: bool = False) -> dict:
        return {"item_id": item_id, "verbose": verbose}

    return _warm(MCPServer(app), {"name": "read_item", "arguments": {"item_id": 7}})


ROUTE_TOOL_SERVER = _build_route_tool_server()


def test_mcp_tools_call_route_backed(benchmark):
    """The full route replay: middleware, hooks, DI, response shaping, teardowns.

    This is the path that makes an exposed route behave the same for an agent as
    for a browser, and the one any change to MCP dispatch is most likely to slow.
    """
    result = benchmark(
        lambda: run_async(
            ROUTE_TOOL_SERVER._tools_call({"name": "read_item", "arguments": {"item_id": 7}})
        )
    )
    assert result["content"]


# ── Listing ────────────────────────────────────────────────


def _build_listing_app() -> Veloce:
    app = Veloce(openapi_url=None)

    for index in range(TOOL_COUNT):

        async def handler(value: int = 0, _index: int = index) -> dict:
            return {"index": _index, "value": value}

        handler.__name__ = f"tool_{index}"
        app.mcp_tool(description=f"Tool number {index}")(handler)

    return app


LISTING_APP = _build_listing_app()
LISTING_SERVER = _warm(MCPServer(LISTING_APP), {"name": "tool_0", "arguments": {"value": 1}})


def test_mcp_tools_list(benchmark):
    """Describe every registered tool — the default, unfiltered listing."""
    result = benchmark(lambda: run_async(LISTING_SERVER._handle_tools_list({})))
    assert len(result["tools"]) == TOOL_COUNT
