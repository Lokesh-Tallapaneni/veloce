"""The argument contract a tool publishes.

A body model declared on a sub-dependency validates against the whole argument
mapping rather than against `arguments[name]`, so its *fields* are the tool's
inputs. The schema declared the parameter name instead, publishing a shape the
call path rejected - a caller following the schema could never succeed.

Unrecognised arguments are still accepted and ignored. That is deliberate: a
route-backed tool can consume a value the schema has no slot to declare (a path
variable a dependency reads through `request.path_params`, for one), so refusing
undeclared names would reject calls that legitimately work.
"""

from __future__ import annotations

import orjson
from pydantic import BaseModel

from veloce import Depends, Veloce
from veloce.contrib.mcp.registry import build_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


class Item(BaseModel):
    name: str
    qty: int = 1


async def _call(server: MCPServer, name: str, arguments: dict) -> tuple[bool, str]:
    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        MCPSession(),
    )
    assert "error" not in response, response
    result = response["result"]
    return bool(result.get("isError")), result["content"][0]["text"]


def _dependency_app() -> Veloce:
    app = Veloce(title="DepProbe", openapi_url=None)

    def parse(item: Item) -> str:
        return f"{item.name} x{item.qty}"

    @app.post("/mk", expose_as_mcp_tool=True, mcp_description="Make an item")
    async def mk(label: str = Depends(parse)) -> dict:
        return {"label": label}

    return app


# ── A sub-dependency's model publishes its fields ────────────────────


def test_a_sub_dependency_model_advertises_its_fields_not_its_parameter():
    schema = build_registry(_dependency_app()).tools["mk"].input_schema
    assert set(schema["properties"]) == {"name", "qty"}
    assert "item" not in schema["properties"]


def test_only_the_model_fields_without_a_default_are_required():
    schema = build_registry(_dependency_app()).tools["mk"].input_schema
    assert schema["required"] == ["name"]


async def test_a_call_following_that_schema_succeeds():
    """The regression this guards: the published shape was unsatisfiable."""
    is_error, text = await _call(MCPServer(_dependency_app()), "mk", {"name": "widget", "qty": 3})
    assert is_error is False
    assert orjson.loads(text) == {"label": "widget x3"}


async def test_a_field_carrying_a_default_may_be_omitted():
    is_error, text = await _call(MCPServer(_dependency_app()), "mk", {"name": "widget"})
    assert is_error is False
    assert orjson.loads(text) == {"label": "widget x1"}


async def test_omitting_a_required_field_is_reported():
    is_error, text = await _call(MCPServer(_dependency_app()), "mk", {"qty": 3})
    assert is_error is True
    assert "name" in text


def test_the_model_fields_carry_their_declared_types():
    schema = build_registry(_dependency_app()).tools["mk"].input_schema
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["qty"]["type"] == "integer"


# ── A top-level model parameter keeps its own name ───────────────────


async def test_a_top_level_model_parameter_still_nests_under_its_name():
    """Only the sub-dependency path spreads; a declared parameter is read from
    `arguments[name]`, so it keeps nesting."""
    app = Veloce(title="TopLevel", openapi_url=None)

    @app.mcp_tool(description="Take an item")
    async def take(item: Item) -> dict:
        return {"name": item.name}

    schema = build_registry(app).tools["take"].input_schema
    assert set(schema["properties"]) == {"item"}

    is_error, text = await _call(MCPServer(app), "take", {"item": {"name": "widget"}})
    assert is_error is False
    assert orjson.loads(text) == {"name": "widget"}


# ── Undeclared arguments stay tolerated ──────────────────────────────


async def test_an_undeclared_argument_is_ignored_not_refused():
    """Refusing one would reject a route-backed tool's path variable, which the
    schema has no slot to declare."""
    app = Veloce(title="Extra", openapi_url=None)

    @app.mcp_tool(description="Search")
    async def search(query: str, limit: int = 10) -> dict:
        return {"query": query, "limit": limit}

    is_error, text = await _call(MCPServer(app), "search", {"query": "cats", "limt": 5})
    assert is_error is False
    assert orjson.loads(text) == {"query": "cats", "limit": 10}


async def test_a_route_backed_tool_accepts_a_path_variable_no_slot_declares():
    """The case that makes strict rejection unsafe: `item_id` is consumed through
    the synthetic request, so no slot exists to advertise it."""
    app = Veloce(title="PathVar", openapi_url=None)

    def read_param(request) -> int:
        return request.path_params["item_id"]

    @app.get("/loc/{item_id}", expose_as_mcp_tool=True, mcp_description="Localised")
    async def loc(value: int = Depends(read_param)) -> dict:
        return {"item_id": value}

    schema = build_registry(app).tools["loc"].input_schema
    assert schema["properties"] == {}

    is_error, text = await _call(MCPServer(app), "loc", {"item_id": 7})
    assert is_error is False
    assert orjson.loads(text) == {"item_id": 7}
