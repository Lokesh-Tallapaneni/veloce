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


async def test_a_path_variable_no_slot_declares_is_advertised_from_the_route():
    """`item_id` is consumed through the synthetic request, and no slot names it.

    The route's template does, so the schema is built from that. Before, the tool
    published no parameters at all: an agent reading it called `loc` with nothing
    and got whatever the dependency does when the value is missing.
    """
    app = Veloce(title="PathVar", openapi_url=None)

    def read_param(request) -> int:
        return request.path_params["item_id"]

    @app.get("/loc/{item_id}", expose_as_mcp_tool=True, mcp_description="Localised")
    async def loc(value: int = Depends(read_param)) -> dict:
        return {"item_id": value}

    schema = build_registry(app).tools["loc"].input_schema
    assert schema["properties"] == {"item_id": {"type": "string"}}
    assert schema["required"] == ["item_id"]

    is_error, text = await _call(MCPServer(app), "loc", {"item_id": 7})
    assert is_error is False
    assert orjson.loads(text) == {"item_id": 7}


async def test_a_typed_path_variable_is_advertised_with_its_converter_type():
    app = Veloce(title="TypedPathVar", openapi_url=None)

    def read_param(request) -> int:
        return request.path_params["item_id"]

    @app.get("/loc/{item_id:int}", expose_as_mcp_tool=True, mcp_description="Localised")
    async def loc(value: int = Depends(read_param)) -> dict:
        return {"item_id": value}

    schema = build_registry(app).tools["loc"].input_schema
    assert schema["properties"] == {"item_id": {"type": "integer"}}


async def test_a_slot_declared_path_parameter_is_not_advertised_twice():
    app = Veloce(title="DeclaredPathVar", openapi_url=None)

    @app.get("/item/{item_id}", expose_as_mcp_tool=True, mcp_description="Declared")
    async def item(item_id: int) -> dict:
        return {"item_id": item_id}

    schema = build_registry(app).tools["item"].input_schema
    # The slot's own annotation wins - it is the more precise of the two.
    assert schema["properties"] == {"item_id": {"type": "integer"}}
    assert schema["required"] == ["item_id"]


async def test_a_raw_query_read_inside_a_dependency_is_still_undeclarable():
    """What still blocks rejecting undeclared arguments.

    A dependency calling `request.query_params.get("q")` consumes an argument
    that no signature and no route template mentions - it is arbitrary code, so
    nothing static can see it. `{"q": ...}` is a legitimate, working call whose
    name the schema cannot carry, and rejecting unknown names would break it.
    """
    app = Veloce(title="RawQuery", openapi_url=None)

    def from_query(request) -> str:
        return request.query_params.get("q", "<absent>")

    @app.mcp_tool(description="Reads a raw query parameter")
    async def pure(value: str = Depends(from_query)) -> dict:
        return {"value": value}

    assert build_registry(app).tools["pure"].input_schema["properties"] == {}

    is_error, text = await _call(MCPServer(app), "pure", {"q": "hello"})
    assert is_error is False
    assert orjson.loads(text) == {"value": "hello"}


# ── The published type is enforced ───────────────────────────────────


def _typed_app() -> Veloce:
    app = Veloce(title="Typed", openapi_url=None)

    @app.mcp_tool(description="Takes one of each")
    async def probe(
        city: str,
        count: int,
        ratio: float = 0.0,
        flag: bool = False,
        nickname: str | None = None,
        payload: dict | None = None,
    ) -> dict:
        return {"types": [type(v).__name__ for v in (city, count, ratio, flag)]}

    return app


async def test_an_object_where_a_number_is_declared_is_refused():
    """It would otherwise reach the handler as a `dict` typed `int`."""
    is_error, text = await _call(MCPServer(_typed_app()), "probe", {"city": "A", "count": {}})
    assert is_error is True
    assert "count" in text


async def test_an_array_where_a_string_is_declared_is_refused():
    is_error, text = await _call(MCPServer(_typed_app()), "probe", {"city": [], "count": 1})
    assert is_error is True
    assert "city" in text


async def test_a_number_where_a_string_is_declared_is_refused():
    is_error, _text = await _call(MCPServer(_typed_app()), "probe", {"city": 5, "count": 1})
    assert is_error is True


async def test_the_refusal_names_the_argument_and_both_types():
    """A model can only correct what it is told; the message is the retry."""
    _is_error, text = await _call(MCPServer(_typed_app()), "probe", {"city": 5, "count": 1})
    assert "city" in text
    assert "expected a string" in text
    assert "got a number" in text


async def test_the_types_are_named_as_json_names_the_model_used():
    _is_error, text = await _call(MCPServer(_typed_app()), "probe", {"city": "A", "count": []})
    assert "expected a number" in text
    assert "got an array" in text


async def test_null_for_a_required_argument_is_refused():
    is_error, _text = await _call(MCPServer(_typed_app()), "probe", {"city": None, "count": 1})
    assert is_error is True


async def test_null_for_an_optional_argument_is_accepted():
    """Its schema carries a null branch, so null is a value the contract allows."""
    is_error, _text = await _call(
        MCPServer(_typed_app()), "probe", {"city": "A", "count": 1, "nickname": None}
    )
    assert is_error is False


async def test_an_object_reaches_a_parameter_declared_to_take_one():
    is_error, _text = await _call(
        MCPServer(_typed_app()), "probe", {"city": "A", "count": 1, "payload": {"k": "v"}}
    )
    assert is_error is False


async def test_a_numeric_string_still_reaches_a_number_parameter():
    """Long-standing leniency: a coercible string is not the failure this closes."""
    is_error, text = await _call(MCPServer(_typed_app()), "probe", {"city": "A", "count": "12"})
    assert is_error is False
    assert orjson.loads(text)["types"] == ["str", "int", "float", "bool"]


async def test_a_route_backed_tool_enforces_it_the_same_way():
    app = Veloce(title="Routed", openapi_url=None)

    @app.get("/echo", expose_as_mcp_tool=True, mcp_description="Echo a word")
    async def echo(word: str) -> dict:
        return {"word": word}

    is_error, text = await _call(MCPServer(app), "echo", {"word": {"not": "a word"}})
    assert is_error is True
    assert "word" in text


# ── Numbers and booleans ─────────────────────────────────────────────


def _numeric_app() -> Veloce:
    app = Veloce(title="Numeric", openapi_url=None)

    @app.mcp_tool(description="Takes a count, a ratio and a flag")
    async def measure(count: int, ratio: float = 0.0, flag: bool = False) -> dict:
        return {
            "count": count,
            "ratio": ratio,
            "flag": flag,
            "types": [type(v).__name__ for v in (count, ratio, flag)],
        }

    return app


async def _measure(arguments: dict) -> tuple[bool, str]:
    return await _call(MCPServer(_numeric_app()), "measure", arguments)


async def test_a_fractional_number_is_refused_where_an_integer_is_declared():
    """It used to arrive as 5, a different value from the one that was sent."""
    is_error, text = await _measure({"count": 5.7})
    assert is_error is True
    assert "fractional" in text


async def test_a_whole_number_written_as_a_float_is_accepted():
    """JSON Schema's `integer` is any number with a zero fractional part."""
    is_error, text = await _measure({"count": 5.0})
    assert is_error is False
    assert orjson.loads(text)["count"] == 5


async def test_a_boolean_is_refused_where_a_number_is_declared():
    """`bool` subclasses `int`, so an unguarded target took `true` as 1."""
    is_error, _text = await _measure({"count": True})
    assert is_error is True


async def test_a_boolean_is_refused_where_a_float_is_declared():
    is_error, _text = await _measure({"count": 1, "ratio": True})
    assert is_error is True


async def test_a_number_is_refused_where_a_boolean_is_declared():
    is_error, _text = await _measure({"count": 1, "flag": 1})
    assert is_error is True


async def test_a_string_is_refused_where_a_boolean_is_declared():
    """`"maybe"` used to arrive as `False`, which is an answer nobody sent."""
    is_error, _text = await _measure({"count": 1, "flag": "maybe"})
    assert is_error is True


async def test_a_numeric_string_still_reaches_a_number():
    """Long-standing leniency, and a common model output; kept deliberately."""
    is_error, text = await _measure({"count": "12"})
    assert is_error is False
    assert orjson.loads(text)["types"] == ["int", "float", "bool"]


async def test_an_integer_still_reaches_a_float_parameter():
    is_error, text = await _measure({"count": 1, "ratio": 2})
    assert is_error is False
    assert orjson.loads(text)["ratio"] == 2.0


async def test_the_refusal_names_the_argument_and_what_was_expected():
    _is_error, text = await _measure({"count": 1, "flag": 1})
    assert "flag" in text
    assert "expected a boolean" in text
    assert "got a number" in text
