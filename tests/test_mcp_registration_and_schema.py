"""Registering a tool, and the input schema that comes out of it.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import asyncio

from tests._mcp import Pipe
from tests._mcp_shared import (
    Customer,
    Item,
    _server,
)
from veloce import (
    MCPContext,
    Veloce,
)
from veloce.contrib.mcp.registry import build_registry

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


def test_shared_handler_two_routes_become_two_tools():
    """One handler mounted as two distinct named routes yields two tools.

    Deduplication is by route, not by the handler callable, so a function
    intentionally mounted twice is not silently collapsed into a single tool.
    """
    app = Veloce(openapi_url=None)

    async def health():
        return {"ok": True}

    app.add_route(
        "/ping",
        health,
        methods=["GET"],
        name="ping",
        expose_as_mcp_tool=True,
        mcp_description="Ping",
    )
    app.add_route(
        "/healthz",
        health,
        methods=["GET"],
        name="healthz",
        expose_as_mcp_tool=True,
        mcp_description="Healthz",
    )

    registry = build_registry(app)
    assert "ping" in registry.tools
    assert "healthz" in registry.tools
    # Both tools wrap the same callable but are independently callable.
    assert registry.tools["ping"].handler is registry.tools["healthz"].handler


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
    # A parameter's default is part of its published contract, so a client can
    # populate the field itself rather than relying on the server's fallback.
    assert props["times"] == {"type": "integer", "default": 1}
    assert props["loud"] == {"type": "boolean", "default": False}
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
    # The ref is local to this standalone schema, and the model's fields are
    # resolvable from `$defs` without any external component envelope.
    ref = schema["properties"]["item"]["$ref"]
    assert ref == "#/$defs/Item"
    assert "Item" in schema["$defs"]
    item_props = schema["$defs"]["Item"]["properties"]
    assert set(item_props) == {"name", "qty"}


def test_pydantic_input_schema_is_self_contained():
    """tools/list inputSchema must resolve model fields with no external lookup."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Create an item")
    async def create(item: Item) -> dict:
        return item.model_dump()

    pipe = Pipe(_server(app))
    pipe.feed({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    out = asyncio.run(pipe.run())

    tool = next(t for t in out[0]["result"]["tools"] if t["name"] == "create")
    schema = tool["inputSchema"]
    ref = schema["properties"]["item"]["$ref"]
    # No dangling OpenAPI component ref: the ref points into the schema's own
    # `$defs`, and that def is present in the same tools/list payload.
    assert ref.startswith("#/$defs/")
    name = ref.split("/")[-1]
    assert name in schema["$defs"]
    assert "components" not in schema
    assert set(schema["$defs"][name]["properties"]) == {"name", "qty"}


def test_nested_pydantic_input_schema_is_self_contained():
    """A model that embeds another model must inline both into `$defs`.

    Pydantic keeps the inner model's ref in its native `#/$defs/<Name>` form
    inside the outer component; the bridge must still pull that component out
    of the shared registry so the tool schema has no dangling ref.
    """
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Create a customer")
    async def create_customer(customer: Customer) -> dict:
        return customer.model_dump()

    registry = build_registry(app)
    schema = registry.tools["create_customer"].input_schema
    defs = schema["$defs"]
    # Both the outer and the nested model are present, with no dangling ref.
    assert "Customer" in defs
    assert "Address" in defs
    inner_ref = defs["Customer"]["properties"]["address"]["$ref"]
    assert inner_ref == "#/$defs/Address"
    assert set(defs["Address"]["properties"]) == {"city", "zip"}


def test_context_param_excluded_from_schema():
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Tool with context")
    async def with_ctx(value: int, ctx: MCPContext) -> int:
        return value

    registry = build_registry(app)
    schema = registry.tools["with_ctx"].input_schema
    assert "value" in schema["properties"]
    assert "ctx" not in schema["properties"]
