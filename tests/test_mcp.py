"""MCP integration tests - registry, schema, stdio round-trip, DI, safety."""

from __future__ import annotations

import asyncio

import orjson
import pytest
from pydantic import BaseModel

from veloce import Blueprint, Depends, MCPContext, Veloce
from veloce.contrib.mcp.registry import build_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.transports.stdio import StdioTransport

# -- In-process stdio driver ------------------------------------------


class _Pipe:
    """Drive a `StdioTransport` in-process: feed request lines, collect replies."""

    def __init__(self, server: MCPServer) -> None:
        self._inbox: list[bytes] = []
        self.outbox: list[dict] = []
        self.transport = StdioTransport(server, self._read_line, self._write_line)

    def feed(self, message: dict) -> None:
        self._inbox.append(orjson.dumps(message))

    async def _read_line(self) -> bytes | None:
        if not self._inbox:
            return None
        return self._inbox.pop(0)

    async def _write_line(self, data: bytes) -> None:
        self.outbox.append(orjson.loads(data))

    async def run(self) -> list[dict]:
        await self.transport.serve()
        return self.outbox


def _server(app: Veloce) -> MCPServer:
    return MCPServer(app)


# Module-level so `get_type_hints` can resolve the annotation (a class defined
# inside a test function is not in the handler's global namespace).
class Item(BaseModel):
    name: str
    qty: int


# -- Registration -----------------------------------------------------


def test_mcp_tool_registration_explicit():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    registry = build_registry(app)
    assert "add" in registry.tools
    tool = registry.tools["add"]
    assert tool.description == "Add two integers"


def test_expose_existing_get_route():
    app = Veloce(openapi_url=None)

    @app.get("/ping", expose_as_mcp_tool=True, mcp_description="Health probe")
    async def ping():
        return {"pong": True}

    registry = build_registry(app)
    assert "ping" in registry.tools
    assert registry.tools["ping"].description == "Health probe"


# -- Schema generation ------------------------------------------------


def test_input_schema_from_signature():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Echo with options")
    async def echo(text: str, times: int = 1, loud: bool = False) -> str:
        return (text.upper() if loud else text) * times

    registry = build_registry(app)
    schema = registry.tools["echo"].input_schema
    assert schema["type"] == "object"
    props = schema["properties"]
    assert props["text"] == {"type": "string"}
    assert props["times"] == {"type": "integer"}
    assert props["loud"] == {"type": "boolean"}
    # `text` is required (no default); `times` / `loud` are not.
    assert schema["required"] == ["text"]


def test_input_schema_with_pydantic_model():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Create an item")
    async def create(item: Item) -> dict:
        return item.model_dump()

    registry = build_registry(app)
    schema = registry.tools["create"].input_schema
    assert "item" in schema["properties"]
    assert schema["properties"]["item"]["$ref"].endswith("/Item")
    assert "Item" in registry.schemas


def test_context_param_excluded_from_schema():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Tool with context")
    async def with_ctx(value: int, ctx: MCPContext) -> int:
        return value

    registry = build_registry(app)
    schema = registry.tools["with_ctx"].input_schema
    assert "value" in schema["properties"]
    assert "ctx" not in schema["properties"]


# -- tools/list + tools/call round-trip -------------------------------


def test_tools_list_and_call_round_trip():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    pipe.feed({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 2, "b": 5}},
        }
    )
    out = asyncio.run(pipe.run())

    init, listed, called = out
    assert init["result"]["serverInfo"]["name"]
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "add" in names
    assert called["result"]["content"][0]["text"] == "7"


def test_call_coerces_string_arguments():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Double an integer")
    async def double(n: int) -> int:
        return n * 2

    pipe = _Pipe(_server(app))
    # JSON argument arrives as a string; the bridge coerces it to int.
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "double", "arguments": {"n": "21"}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "42"


def test_call_pydantic_body_model():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Summarise an item")
    async def summarise(item: Item) -> str:
        return f"{item.qty}x {item.name}"

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "summarise", "arguments": {"item": {"name": "widget", "qty": 3}}},
        }
    )
    out = asyncio.run(pipe.run())
    assert out[0]["result"]["content"][0]["text"] == "3x widget"


def test_unknown_method_returns_method_not_found():
    app = Veloce(openapi_url=None)
    pipe = _Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 9, "method": "does/not/exist", "params": {}})
    out = asyncio.run(pipe.run())
    assert out[0]["error"]["code"] == -32601


def test_notification_yields_no_response():
    app = Veloce(openapi_url=None)
    pipe = _Pipe(_server(app))
    # No `id` -> notification, no response written.
    pipe.feed({"jsonrpc": "2.0", "method": "notifications/initialized"})
    out = asyncio.run(pipe.run())
    assert out == []


def test_parse_error_on_bad_json():
    app = Veloce(openapi_url=None)
    server = _server(app)
    transport = StdioTransport(server, None, None)  # type: ignore[arg-type]
    out = asyncio.run(transport._dispatch_line(b"{not json"))
    assert out["error"]["code"] == -32700


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

    pipe = _Pipe(_server(app))
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

    pipe = _Pipe(_server(app))
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

    pipe = _Pipe(_server(app))
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


def test_sync_handler_offloaded():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Sync add")
    def sync_add(a: int, b: int) -> int:
        return a + b

    pipe = _Pipe(_server(app))
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


# -- Safety rules -----------------------------------------------------


def test_post_route_not_auto_exposed():
    app = Veloce(openapi_url=None)

    # A POST route with no explicit opt-in must NOT become a tool.
    @app.post("/items")
    async def create_item():
        return {"ok": True}

    registry = build_registry(app)
    assert registry.tools == {}


def test_post_route_explicit_opt_in_exposed():
    app = Veloce(openapi_url=None)

    @app.post("/items", expose_as_mcp_tool=True, mcp_description="Create an item")
    async def create_item():
        return {"ok": True}

    registry = build_registry(app)
    assert "create_item" in registry.tools


def test_missing_mcp_description_raises_on_exposed_route():
    app = Veloce(openapi_url=None)

    @app.get("/x", expose_as_mcp_tool=True)
    async def x():
        return {}

    with pytest.raises(ValueError, match="description"):
        build_registry(app)


def test_missing_description_raises_on_mcp_tool_decorator():
    app = Veloce(openapi_url=None)

    with pytest.raises(ValueError, match="description"):

        @app.mcp_tool(description="")
        async def bad():
            return None


# -- Blueprint namespacing --------------------------------------------


def test_blueprint_namespacing():
    app = Veloce(openapi_url=None)
    bp = Blueprint("billing", url_prefix="/billing")

    @bp.get("/status", expose_as_mcp_tool=True, mcp_description="Billing status")
    async def status():
        return {"ok": True}

    app.register_blueprint(bp)

    registry = build_registry(app)
    # Blueprint route name is `billing.status` -> tool name `billing_status`.
    assert "billing_status" in registry.tools


def test_mcp_tool_namespace_prefix():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Namespaced add", namespace="math")
    async def add(a: int, b: int) -> int:
        return a + b

    registry = build_registry(app)
    assert "math_add" in registry.tools


# -- Instrumentation --------------------------------------------------


def test_instrumentation_fires_on_tool_call():
    app = Veloce(openapi_url=None)
    seen: list[tuple[str, str | None]] = []

    @app.add_instrumentation
    def record(metrics):
        seen.append((metrics.method, metrics.route))

    @app.mcp_tool(description="Add")
    async def add(a: int, b: int) -> int:
        return a + b

    pipe = _Pipe(_server(app))
    pipe.feed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add", "arguments": {"a": 1, "b": 2}},
        }
    )
    asyncio.run(pipe.run())
    assert ("tools/call", "add") in seen


def test_mount_mcp_rejects_unknown_transport():
    app = Veloce(openapi_url=None)
    with pytest.raises(ValueError, match="transport"):
        app.mount_mcp(transport="http")


def test_duplicate_tool_name_raises():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="One")
    async def dup():
        return 1

    # A second tool resolving to the same name collides at registry build.
    app._mcp_tools.append((dup, "dup", "Two", None))
    with pytest.raises(ValueError, match="Duplicate"):
        build_registry(app)
