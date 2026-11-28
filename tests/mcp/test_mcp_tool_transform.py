"""Deriving a narrower tool from one that already exists.

An internal tool is often nearly the tool worth publishing, differing only in
what an agent should see: a friendlier argument name, a credential the caller
must not supply, a limit it must not raise. Getting that meant rewriting the
handler or restating its schema by hand.

`derive_tool` changes the published surface and translates back before the
original handler runs, so both tools share one implementation.
"""

from __future__ import annotations

import orjson
import pytest

from veloce import Veloce
from veloce.contrib.mcp.registry import build_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.transform import ArgTransform, derive_tool

SERVER_KEY = "server-held-key"


def _app() -> tuple[Veloce, object]:
    app = Veloce(title="Derived", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Search the index (internal)")
    async def search(query: str, api_key: str, limit: int = 5) -> dict:
        return {"query": query, "limit": limit, "key": api_key}

    return app, build_registry(app).tools["search"]


def _published(app: Veloce, name: str) -> dict:
    return build_registry(app).tools[name].input_schema


async def _call(app: Veloce, name: str, arguments: dict) -> dict:
    response = await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        MCPSession(),
    )
    result = response["result"]
    assert not result.get("isError"), result["content"][0]["text"]
    return orjson.loads(result["content"][0]["text"])


# ── Renaming ─────────────────────────────────────────────────────────


async def test_an_argument_can_be_published_under_another_name():
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(internal, name="public", arguments={"query": ArgTransform(name="q")})
    )
    assert "q" in _published(app, "public")["properties"]
    assert "query" not in _published(app, "public")["properties"]


async def test_the_renamed_value_reaches_the_original_parameter():
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(
            internal,
            name="public",
            arguments={
                "query": ArgTransform(name="q"),
                "api_key": ArgTransform(hide=True, default=SERVER_KEY),
            },
        )
    )
    assert (await _call(app, "public", {"q": "cats"}))["query"] == "cats"


# ── Hiding ───────────────────────────────────────────────────────────


async def test_a_hidden_argument_is_not_published():
    """How a server-held credential stays server-held."""
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(
            internal,
            name="public",
            arguments={"api_key": ArgTransform(hide=True, default=SERVER_KEY)},
        )
    )
    assert "api_key" not in _published(app, "public")["properties"]
    assert "api_key" not in _published(app, "public").get("required", [])


async def test_a_hidden_argument_is_supplied_on_every_call():
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(
            internal,
            name="public",
            arguments={"api_key": ArgTransform(hide=True, default=SERVER_KEY)},
        )
    )
    assert (await _call(app, "public", {"query": "cats"}))["key"] == SERVER_KEY


def test_hiding_an_argument_without_a_default_is_refused():
    """The caller cannot supply it, so the handler would be called without it."""
    with pytest.raises(ValueError, match="hidden argument needs a default"):
        ArgTransform(hide=True)


def test_renaming_a_hidden_argument_is_refused():
    with pytest.raises(ValueError, match="not published"):
        ArgTransform(hide=True, default=1, name="other")


# ── Defaults and requiredness ────────────────────────────────────────


async def test_a_derived_default_applies_when_the_caller_omits_it():
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(
            internal,
            name="public",
            arguments={
                "api_key": ArgTransform(hide=True, default=SERVER_KEY),
                "limit": ArgTransform(default=10),
            },
        )
    )
    assert (await _call(app, "public", {"query": "cats"}))["limit"] == 10


async def test_the_caller_may_still_override_a_derived_default():
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(
            internal,
            name="public",
            arguments={
                "api_key": ArgTransform(hide=True, default=SERVER_KEY),
                "limit": ArgTransform(default=10),
            },
        )
    )
    assert (await _call(app, "public", {"query": "cats", "limit": 3}))["limit"] == 3


def test_supplying_a_default_makes_a_required_argument_optional():
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(internal, name="public", arguments={"query": ArgTransform(default="all")})
    )
    assert "query" not in _published(app, "public").get("required", [])


def test_requiredness_can_be_stated_outright():
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(internal, name="public", arguments={"limit": ArgTransform(required=True)})
    )
    assert "limit" in _published(app, "public")["required"]


# ── Descriptions and schemas ─────────────────────────────────────────


def test_an_argument_description_can_be_rewritten():
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(
            internal, name="public", arguments={"query": ArgTransform(description="What to find")}
        )
    )
    assert _published(app, "public")["properties"]["query"]["description"] == "What to find"


def test_an_argument_schema_can_be_replaced():
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(
            internal,
            name="public",
            arguments={"limit": ArgTransform(schema={"type": "integer", "maximum": 20})},
        )
    )
    assert _published(app, "public")["properties"]["limit"]["maximum"] == 20


def test_an_untouched_argument_is_published_unchanged():
    app, internal = _app()
    derived = derive_tool(internal, name="public", arguments={"query": ArgTransform(name="q")})
    assert (
        derived.input_schema["properties"]["limit"] == internal.input_schema["properties"]["limit"]
    )


# ── The original is untouched ────────────────────────────────────────


async def test_the_original_tool_keeps_working():
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(
            internal,
            name="public",
            arguments={"api_key": ArgTransform(hide=True, default=SERVER_KEY)},
        )
    )
    answer = await _call(app, "search", {"query": "dogs", "api_key": "caller-key"})
    assert answer["key"] == "caller-key"


def test_the_original_schema_is_not_mutated():
    _app_obj, internal = _app()
    before = {**internal.input_schema["properties"]}
    derive_tool(internal, name="public", arguments={"query": ArgTransform(name="q")})
    assert internal.input_schema["properties"] == before


def test_both_tools_are_listed():
    app, internal = _app()
    app.add_mcp_tool(derive_tool(internal, name="public"))
    assert sorted(build_registry(app).tools) == ["public", "search"]


# ── Mistakes are refused ─────────────────────────────────────────────


def test_naming_an_argument_the_tool_does_not_have_is_refused():
    """Silently doing nothing would hide the typo until an agent called it."""
    _app_obj, internal = _app()
    with pytest.raises(ValueError, match="no argument"):
        derive_tool(internal, name="public", arguments={"nonexistent": ArgTransform(name="x")})


def test_the_refusal_lists_the_arguments_that_do_exist():
    _app_obj, internal = _app()
    with pytest.raises(ValueError, match="api_key"):
        derive_tool(internal, name="public", arguments={"typo": ArgTransform(name="x")})


# ── Derivation with nothing changed ──────────────────────────────────


def test_deriving_without_changes_republishes_the_same_surface():
    _app_obj, internal = _app()
    derived = derive_tool(internal, name="alias")
    assert derived.input_schema["properties"] == internal.input_schema["properties"]
    assert sorted(derived.input_schema["required"]) == sorted(internal.input_schema["required"])


def test_a_derived_tool_may_restate_its_description():
    _app_obj, internal = _app()
    derived = derive_tool(internal, name="public", description="Search the catalogue")
    assert derived.description == "Search the catalogue"
    assert internal.description == "Search the index (internal)"


# ── Deriving from a derived tool ─────────────────────────────────────


def test_deriving_from_a_derived_tool_is_refused():
    """It published a plausible schema, registered cleanly, and failed every call."""
    _app_obj, internal = _app()
    once = derive_tool(internal, name="public", arguments={"query": ArgTransform(name="q")})
    with pytest.raises(ValueError, match="already a derived tool"):
        derive_tool(once, name="find", arguments={"q": ArgTransform(name="text")})


def test_the_refusal_says_what_to_derive_from_instead():
    _app_obj, internal = _app()
    once = derive_tool(internal, name="public")
    with pytest.raises(ValueError, match="derive from the original"):
        derive_tool(once, name="find")


def test_deriving_twice_from_the_same_original_is_fine():
    """Two façades over one handler is the supported shape."""
    app, internal = _app()
    app.add_mcp_tool(derive_tool(internal, name="one", arguments={"query": ArgTransform(name="q")}))
    app.add_mcp_tool(derive_tool(internal, name="two", arguments={"query": ArgTransform(name="s")}))
    assert "q" in _published(app, "one")["properties"]
    assert "s" in _published(app, "two")["properties"]


# ── A replacement schema the handler would refuse ────────────────────


def test_offering_a_type_the_handler_does_not_take_is_refused():
    """It advertised `integer`, rejected an integer, and accepted a string."""
    _app_obj, internal = _app()
    with pytest.raises(ValueError, match="publishes a shape the tool would refuse"):
        derive_tool(
            internal,
            name="public",
            arguments={"query": ArgTransform(schema={"type": "integer"})},
        )


def test_the_transform_refusal_names_the_argument_and_both_types():
    _app_obj, internal = _app()
    with pytest.raises(ValueError, match="'query'.*string.*integer"):
        derive_tool(
            internal,
            name="public",
            arguments={"query": ArgTransform(schema={"type": "integer"})},
        )


def test_narrowing_within_the_declared_type_is_allowed():
    """An enum or a bound is what the argument is for."""
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(
            internal,
            name="public",
            arguments={"query": ArgTransform(schema={"type": "string", "enum": ["cats", "dogs"]})},
        )
    )
    assert _published(app, "public")["properties"]["query"]["enum"] == ["cats", "dogs"]


def test_a_bound_on_a_number_is_allowed():
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(
            internal,
            name="public",
            arguments={"limit": ArgTransform(schema={"type": "integer", "maximum": 20})},
        )
    )
    assert _published(app, "public")["properties"]["limit"]["maximum"] == 20


def test_a_schema_naming_no_type_is_left_alone():
    """A `$ref` or a bare constraint says nothing a compatibility check can read."""
    app, internal = _app()
    app.add_mcp_tool(
        derive_tool(
            internal,
            name="public",
            arguments={"query": ArgTransform(schema={"description": "anything"})},
        )
    )
    assert _published(app, "public")["properties"]["query"] == {"description": "anything"}


def test_an_optional_parameters_null_branch_is_understood():
    """Its published shape is an `anyOf`, not a plain `type`."""
    app = Veloce(title="Optional", openapi_url=None)

    @app.mcp_tool(description="Takes an optional note")
    async def note(text: str | None = None) -> dict:
        return {"text": text}

    internal = build_registry(app).tools["note"]
    app.add_mcp_tool(
        derive_tool(
            internal,
            name="public",
            arguments={"text": ArgTransform(schema={"type": "string", "maxLength": 10})},
        )
    )
    assert _published(app, "public")["properties"]["text"]["maxLength"] == 10


def test_offering_a_type_outside_an_optional_parameters_branches_is_refused():
    app = Veloce(title="Optional", openapi_url=None)

    @app.mcp_tool(description="Takes an optional note")
    async def note(text: str | None = None) -> dict:
        return {"text": text}

    with pytest.raises(ValueError, match="publishes a shape the tool would refuse"):
        derive_tool(
            build_registry(app).tools["note"],
            name="public",
            arguments={"text": ArgTransform(schema={"type": "array"})},
        )
