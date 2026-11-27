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

import asyncio
from typing import Annotated, Any

import orjson
import pytest
from pydantic import BaseModel, Field

from veloce import Body, Depends, Query, TestClient, Veloce
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


async def test_the_binding_refusal_names_the_argument_and_both_types():
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


# ── Array arguments are as strict as scalar ones ─────────────────────
#
# The binder refuses a wrong scalar type on the stated grounds that a mismatch
# is the model's mistake to correct and passing it through leaves the handler
# holding a type it never declared. The array branch used to wrap ANY non-list
# in a one-element list instead, so a tool told to filter by `'["a","b"]'` - a
# shape models really do send - filtered by one nonsense tag and returned a
# plausible empty result, with nothing in the trace to say why.


def _array_app() -> Veloce:
    app = Veloce(title="ArrayProbe", openapi_url=None)

    @app.mcp_tool(description="Filter by tags")
    async def search(tags: list[str]) -> dict:
        return {"tags": tags, "types": [type(t).__name__ for t in tags]}

    @app.mcp_tool(description="Sum some numbers")
    async def total(values: list[int]) -> dict:
        return {"values": values}

    @app.mcp_tool(description="Filter by optional tags")
    async def optional_tags(tags: list[str] | None = None) -> dict:
        return {"tags": tags}

    @app.mcp_tool(description="Take a list whose members may be null")
    async def nullable_members(items: list[str | None]) -> dict:
        return {"items": items}

    @app.mcp_tool(description="Take a bounded list whose members may be null")
    async def bounded_nullable_members(
        items: Annotated[list[int | None], Field(min_length=1, ge=0)],
    ) -> dict:
        return {"items": items}

    @app.mcp_tool(description="Take an explicitly marked bounded nullable list")
    async def marked_nullable_members(
        items: Annotated[list[int | None], Query(ge=0)],
    ) -> dict:
        return {"items": items}

    @app.mcp_tool(description="Take a marked list carrying no bounds")
    @app.get("/unbounded")
    async def unbounded_marked(ids: list[int] = Query(default_factory=list)) -> dict:
        return {"ids": ids, "types": [type(v).__name__ for v in ids]}

    return app


def _array_server() -> MCPServer:
    return MCPServer(_array_app())


@pytest.mark.parametrize(
    "value",
    [
        '["a","b"]',  # the stringified array a model most often sends
        "a",
        42,
        4.5,
        True,
        {"tags": ["a"]},
    ],
)
async def test_a_non_array_is_refused_rather_than_wrapped(value):
    failed, message = await _call(_array_server(), "search", {"tags": value})
    assert failed
    assert "expected an array" in message


async def test_an_actual_array_is_still_accepted():
    failed, message = await _call(_array_server(), "search", {"tags": ["a", "b"]})
    assert not failed
    assert orjson.loads(message)["tags"] == ["a", "b"]


async def test_an_empty_array_is_accepted():
    """Empty is a legitimate array, not a missing one."""
    failed, message = await _call(_array_server(), "search", {"tags": []})
    assert not failed
    assert orjson.loads(message)["tags"] == []


@pytest.mark.parametrize("member", [42, 4.5, True, ["nested"], {"k": "v"}])
async def test_a_member_of_the_wrong_type_is_refused(member):
    """`list[str]` means every member is a string, not just the first."""
    failed, message = await _call(_array_server(), "search", {"tags": ["ok", member]})
    assert failed
    assert "expected a string" in message


async def test_a_null_member_is_refused_when_the_inner_type_is_not_nullable():
    failed, _ = await _call(_array_server(), "search", {"tags": [None]})
    assert failed


async def test_a_null_member_is_accepted_when_the_inner_type_is_nullable():
    """`list[str | None]` declares members that may be null; they are."""
    failed, message = await _call(_array_server(), "nullable_members", {"items": ["a", None]})
    assert not failed
    assert orjson.loads(message)["items"] == ["a", None]


async def test_a_nullable_member_list_still_refuses_a_wrong_type():
    failed, _ = await _call(_array_server(), "nullable_members", {"items": [42]})
    assert failed


async def test_a_bound_does_not_make_a_nullable_member_refused():
    """A `Field` bounds the list; it does not withdraw the declared null member.

    The bounds ran against the raw values rather than the coerced ones, which
    re-read each member against the still-optional inner type and refused the
    null this signature declares.
    """
    failed, message = await _call(
        _array_server(), "bounded_nullable_members", {"items": [1, None, 3]}
    )
    assert not failed, message
    assert orjson.loads(message)["items"] == [1, None, 3]


async def test_a_bounded_nullable_member_list_still_enforces_the_item_count():
    failed, message = await _call(_array_server(), "bounded_nullable_members", {"items": []})
    assert failed
    assert "at least 1 items" in message


async def test_a_bounded_nullable_member_list_still_enforces_the_member_bound():
    failed, message = await _call(_array_server(), "bounded_nullable_members", {"items": [-1]})
    assert failed
    assert ">= 0" in message


async def test_an_explicit_marker_bounds_a_nullable_member_list_the_same_way():
    """The two spellings of a bounded list must accept the same values.

    The explicit marker coerced each member against the declared member type
    without unwrapping it, so `list[int | None]` refused the null that the
    folded `Field` spelling of the same signature accepted.
    """
    failed, message = await _call(_array_server(), "marked_nullable_members", {"items": [1, None]})
    assert not failed, message
    assert orjson.loads(message)["items"] == [1, None]


async def test_an_explicit_marker_on_a_nullable_member_list_still_bounds_members():
    failed, message = await _call(_array_server(), "marked_nullable_members", {"items": [-1]})
    assert failed
    assert ">= 0" in message


async def test_an_optional_array_accepts_an_explicit_null():
    """The parameter itself is nullable, so the array is simply absent."""
    failed, message = await _call(_array_server(), "optional_tags", {"tags": None})
    assert not failed
    assert orjson.loads(message)["tags"] is None


async def test_an_optional_array_may_be_omitted_entirely():
    failed, message = await _call(_array_server(), "optional_tags", {})
    assert not failed
    assert orjson.loads(message)["tags"] is None


async def test_an_optional_array_still_refuses_a_non_array():
    """Nullable is not the same as untyped."""
    failed, message = await _call(_array_server(), "optional_tags", {"tags": "a"})
    assert failed
    assert "expected an array" in message


async def test_an_optional_array_still_refuses_a_null_member():
    """The parameter's nullability is not its members'."""
    failed, _ = await _call(_array_server(), "optional_tags", {"tags": [None]})
    assert failed


async def test_array_members_get_the_same_coercion_a_bare_parameter_gets():
    """Strictness means matching the scalar contract, not exceeding it."""
    failed, message = await _call(_array_server(), "total", {"values": [7, 8.0]})
    assert not failed
    assert orjson.loads(message)["values"] == [7, 8]


async def test_a_member_that_would_lose_precision_is_refused():
    failed, message = await _call(_array_server(), "total", {"values": [7.5]})
    assert failed
    assert "fractional" in message


async def test_a_declared_member_model_is_validated_onto_it():
    """`list[Model]` members are models, not the raw mappings that were sent."""
    app = Veloce(title="ModelArray", openapi_url=None)

    @app.mcp_tool(description="Take several items")
    async def take(items: list[Item]) -> dict:
        return {"kinds": [type(i).__name__ for i in items], "names": [i.name for i in items]}

    failed, message = await _call(
        MCPServer(app), "take", {"items": [{"name": "a"}, {"name": "b", "qty": 3}]}
    )
    assert not failed
    assert orjson.loads(message) == {"kinds": ["Item", "Item"], "names": ["a", "b"]}


async def test_a_member_model_that_does_not_validate_is_reported():
    app = Veloce(title="ModelArray", openapi_url=None)

    @app.mcp_tool(description="Take several items")
    async def take(items: list[Item]) -> dict:
        return {"count": len(items)}

    failed, _ = await _call(MCPServer(app), "take", {"items": [{"qty": 1}]})
    assert failed


# ── A parameter behind Depends is held to the schema that published it ──
#
# The MCP door coerces its own top-level slots strictly: an agent sends typed
# JSON, so a parameter declared `bool` takes `true`, not `"yes"`. A parameter
# declared inside a `Depends` was seeded onto the synthetic request instead and
# read back by the HTTP resolver, which applies query-string rules - where "1"
# and "yes" have to mean true because a query string has nothing else to offer.
#
# Both are published in the same `inputSchema`, so the tool advertised
# `{"type": "boolean"}` and then accepted a string for it. Moving a parameter
# behind a dependency silently changed whether a value was accepted.


def _both_doors_app() -> Veloce:
    app = Veloce(openapi_url=None)

    def flag_dep(flag: bool = Query(False)):
        return flag

    @app.mcp_tool(description="Bool declared on the handler")
    async def top(flag: bool = False) -> dict:
        return {"flag": flag}

    @app.mcp_tool(description="Bool declared inside a dependency")
    async def nested(flag: bool = Depends(flag_dep)) -> dict:
        return {"flag": flag}

    return app


async def _call_flag(server: MCPServer, tool: str, value: object):
    out = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {"flag": value}},
        }
    )
    result = out.get("result", {})
    if result.get("isError"):
        return "refused"
    return orjson.loads(result["content"][0]["text"])["flag"]


def test_both_doors_publish_the_parameter():
    """The premise: the dependency's parameter is part of the tool's contract."""
    registry = build_registry(_both_doors_app())
    for name in ("top", "nested"):
        props = registry.tools[name].input_schema["properties"]
        assert props["flag"]["type"] == "boolean", name


@pytest.mark.parametrize("value", ["yes", "1", "true", 1, 0, "maybe"])
async def test_a_declared_bool_refuses_a_non_boolean_behind_depends(value):
    """The defect: these were accepted behind a dependency and refused in front."""
    server = MCPServer(_both_doors_app())
    assert await _call_flag(server, "nested", value) == "refused"


@pytest.mark.parametrize("value", ["yes", "1", "true", 1, 0, "maybe"])
async def test_both_doors_agree_on_every_rejected_value(value):
    """Agreement is half of it; the other half is *what* they agree on.

    Asserting only that the two answers match passes just as well if both
    doors accept the value, which is the failure this module exists to rule
    out.
    """
    server = MCPServer(_both_doors_app())
    top = await _call_flag(server, "top", value)
    nested = await _call_flag(server, "nested", value)
    assert top == nested
    assert top == "refused", f"{value!r} was accepted by both doors"


@pytest.mark.parametrize("value", [True, False])
async def test_a_real_boolean_is_accepted_by_both_doors(value):
    """Strictness must not cost the tool its actual contract."""
    server = MCPServer(_both_doors_app())
    assert await _call_flag(server, "top", value) is value
    assert await _call_flag(server, "nested", value) is value


async def test_a_tool_with_no_dependency_inputs_is_unaffected():
    server = MCPServer(_both_doors_app())
    assert await _call_flag(server, "top", True) is True


# ── A marker carrying no bounds is still a declared element type ─────
#
# Element coercion was reached only when the marker also carried `minItems` /
# `maxItems` / an element bound, because the bound was what the branch was
# written for. A marker declaring nothing but `list[int]` fell through to
# `marker.validate`, whose guards skip a list, so the handler was handed the
# raw JSON members - `["1", "2"]` for a declared `list[int]`. The declared type
# is the contract whether or not a bound accompanies it.


async def test_an_unbounded_marked_list_coerces_its_members():
    is_error, text = await _call(_array_server(), "unbounded_marked", {"ids": ["1", "2"]})
    assert not is_error, text
    assert orjson.loads(text) == {"ids": [1, 2], "types": ["int", "int"]}


async def test_an_unbounded_marked_list_refuses_a_member_of_the_wrong_type():
    """The negative: coercing must not mean accepting anything."""
    is_error, text = await _call(_array_server(), "unbounded_marked", {"ids": ["abc"]})
    assert is_error
    assert "ids" in text


async def test_a_bounded_marked_list_still_coerces_its_members():
    """The case that already worked, so the shared path keeps working."""
    is_error, text = await _call(_array_server(), "marked_nullable_members", {"items": ["1", None]})
    assert not is_error, text
    assert orjson.loads(text) == {"items": [1, None]}


def test_both_doors_agree_on_an_unbounded_marked_list():
    """The invariant the divergence broke: one handler, two doors, one answer.

    Sync so the test client owns the loop it drives the HTTP door on; the MCP
    call is run on its own.
    """
    app = _array_app()
    is_error, text = asyncio.run(_call(MCPServer(app), "unbounded_marked", {"ids": ["1", "2"]}))
    assert not is_error, text
    over_mcp = orjson.loads(text)
    with TestClient(app) as client:
        over_http = client.get("/unbounded?ids=1&ids=2").json()
    assert over_mcp == over_http


async def test_a_body_list_bound_names_body_not_query_in_its_error():
    """The agent reads the location: an MCP call has no query string to name.

    Both the list-bound and the scalar-bound reports on this door were rooted
    at `"query"` for every marker kind, so a `Body(...)` violation pointed the
    model at a source it had never sent.
    """
    app = Veloce(title="LocProbe", openapi_url=None)

    @app.mcp_tool(description="Take a bounded body list")
    @app.post("/bulk")
    async def bulk(payload: list[int] = Body(max_length=2)) -> dict:
        return {"n": len(payload)}

    is_error, text = await _call(MCPServer(app), "bulk", {"payload": [1, 2, 3]})
    assert is_error
    # The rendered text names the parameter rather than the loc root, so the
    # assertion is that no wrong source is named - which is what the agent
    # would otherwise be sent chasing.
    assert "query" not in text
    assert "payload" in text


# ── the agent door reads the same declared shapes ────────────────────


async def test_a_fixed_shape_tuple_argument_is_coerced_per_position():
    """Holding every member to the first type argument refused the second."""
    app = Veloce(title="ShapeProbe", openapi_url=None)

    @app.mcp_tool(description="Take a pair")
    async def pair(payload: tuple[int, str]) -> dict:
        return {"payload": list(payload), "types": [type(v).__name__ for v in payload]}

    is_error, text = await _call(MCPServer(app), "pair", {"payload": [1, "a"]})
    assert not is_error, text
    assert orjson.loads(text) == {"payload": [1, "a"], "types": ["int", "str"]}


async def test_a_fixed_shape_tuple_argument_still_refuses_a_wrong_position():
    app = Veloce(title="ShapeProbe2", openapi_url=None)

    @app.mcp_tool(description="Take a pair")
    async def pair(payload: tuple[int, str] = Query()) -> dict:
        return {"payload": list(payload)}

    is_error, _text = await _call(MCPServer(app), "pair", {"payload": ["a", "b"]})
    assert is_error


async def test_a_fixed_shape_tuple_argument_refuses_the_wrong_length():
    app = Veloce(title="ShapeProbe3", openapi_url=None)

    @app.mcp_tool(description="Take a pair")
    async def pair(payload: tuple[int, str] = Query()) -> dict:
        return {"payload": list(payload)}

    is_error, text = await _call(MCPServer(app), "pair", {"payload": [1]})
    assert is_error
    assert "2" in text


async def test_an_any_typed_list_argument_accepts_a_null_member():
    """`Any` declares nothing to check, `None` included.

    Making member coercion unconditional reached `list[Any]`, whose members had
    no declared type to coerce onto - and a null was refused as "expected a Any".
    """
    app = Veloce(title="AnyProbe", openapi_url=None)

    @app.mcp_tool(description="Take anything")
    async def anything(vals: list[Any] = Query()) -> dict:
        return {"vals": vals}

    is_error, text = await _call(MCPServer(app), "anything", {"vals": [1, None, "x"]})
    assert not is_error, text
    assert orjson.loads(text) == {"vals": [1, None, "x"]}


async def test_an_any_typed_list_argument_still_requires_an_array():
    """The negative: `Any` members do not make the container itself optional."""
    app = Veloce(title="AnyProbe2", openapi_url=None)

    @app.mcp_tool(description="Take anything")
    async def anything(vals: list[Any] = Query()) -> dict:
        return {"vals": vals}

    is_error, _text = await _call(MCPServer(app), "anything", {"vals": "not-an-array"})
    assert is_error


# ── a member that is itself a sequence ───────────────────────────────
#
# A nested member has its own members and its own container. Falling through to
# the scalar branch handed the handler the raw list the decoder produced, so a
# declared tuple member arrived mutable - and a `set` built from those lists
# then raised the interpreter's own "unhashable type", which reached the agent
# as a server fault where the HTTP door answers a validation error.


async def test_a_nested_tuple_member_arrives_as_a_tuple():
    app = Veloce(title="NestProbe", openapi_url=None)

    @app.mcp_tool(description="pairs")
    async def pairs(payload: list[tuple[int, int]] = Query()) -> dict:
        return {"types": [type(item).__name__ for item in payload]}

    is_error, text = await _call(MCPServer(app), "pairs", {"payload": [[1, 2]]})
    assert not is_error, text
    assert orjson.loads(text) == {"types": ["tuple"]}


async def test_a_nested_member_of_the_wrong_shape_is_refused():
    app = Veloce(title="NestProbe2", openapi_url=None)

    @app.mcp_tool(description="pairs")
    async def pairs(payload: list[tuple[int, int]] = Query()) -> dict:
        return {"n": len(payload)}

    is_error, _text = await _call(MCPServer(app), "pairs", {"payload": [[1, 2], 2]})
    assert is_error


async def test_a_set_of_hashable_nested_members_is_built():
    app = Veloce(title="NestProbe3", openapi_url=None)

    @app.mcp_tool(description="pairset")
    async def pairset(payload: set[tuple[int, int]] = Query()) -> dict:
        return {"n": len(payload)}

    is_error, text = await _call(MCPServer(app), "pairset", {"payload": [[1, 2], [1, 2]]})
    assert not is_error, text
    assert orjson.loads(text) == {"n": 1}


async def test_a_set_of_unhashable_members_is_a_validation_error():
    """Not the interpreter's own text, and not a server fault."""
    app = Veloce(title="NestProbe4", openapi_url=None)

    @app.mcp_tool(description="dictset")
    async def dictset(payload: set[dict] = Query()) -> dict:
        return {"n": len(payload)}

    is_error, text = await _call(MCPServer(app), "dictset", {"payload": [{"a": 1}]})
    assert is_error
    assert "unhashable" not in text
    assert "payload" in text


# ── a set's bounds are read after its duplicates are dropped ─────────


async def test_a_set_bound_is_read_after_deduplication():
    """The HTTP door bounds the set; this door bounded the list it came from.

    `min_length=2` then admitted `["a", "a"]` as a one-member set, below the
    minimum it published, and `max_length` refused what HTTP accepted.
    """
    app = Veloce(title="BoundProbe", openapi_url=None)

    @app.mcp_tool(description="bounded")
    async def bounded(payload: set[str] = Query(min_length=2)) -> dict:
        return {"n": len(payload)}

    is_error, text = await _call(MCPServer(app), "bounded", {"payload": ["a", "a"]})
    assert is_error
    assert "at least 2" in text


async def test_a_set_within_its_bound_after_deduplication_is_accepted():
    app = Veloce(title="BoundProbe2", openapi_url=None)

    @app.mcp_tool(description="bounded")
    async def bounded(payload: set[str] = Query(max_length=2)) -> dict:
        return {"n": len(payload)}

    is_error, text = await _call(MCPServer(app), "bounded", {"payload": ["a", "a", "b"]})
    assert not is_error, text
    assert orjson.loads(text) == {"n": 2}


# ── a union member that includes null ────────────────────────────────


async def test_a_null_member_of_a_wide_union_is_accepted():
    """`_unwrap_optional` reads `T | None`; a wider union carries its `None`
    as one member among several, and reading only the narrow form refused a
    null the signature allows."""
    app = Veloce(title="UnionProbe", openapi_url=None)

    @app.mcp_tool(description="wide")
    async def wide(vals: list[int | str | None] = Query()) -> dict:
        return {"vals": vals}

    is_error, text = await _call(MCPServer(app), "wide", {"vals": [1, "a", None]})
    assert not is_error, text
    assert orjson.loads(text) == {"vals": [1, "a", None]}


async def test_a_wide_union_still_refuses_a_member_it_does_not_name():
    app = Veloce(title="UnionProbe2", openapi_url=None)

    @app.mcp_tool(description="wide")
    async def wide(vals: list[int | str | None] = Query()) -> dict:
        return {"vals": vals}

    is_error, text = await _call(MCPServer(app), "wide", {"vals": [{}]})
    assert is_error
    assert "Union" not in text
