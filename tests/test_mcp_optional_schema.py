"""What an optional parameter publishes.

The call path has always accepted an explicit null for an optional parameter and
an omission for one carrying a default. The schema said neither: an
`Optional[str]` was advertised as a plain string, so the published type rejected
a value the server takes, and a default never appeared at all, so a client had
to omit the field and hope. Both are now declared.
"""

from __future__ import annotations

import orjson
import pytest
from pydantic import BaseModel

from veloce import Query, Veloce
from veloce.contrib.mcp.registry import build_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


def _app() -> Veloce:
    app = Veloce(title="OptProbe", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Every optional shape")
    async def probe(
        required: int,
        legacy_optional: str | None = None,
        union_optional: str | None = None,
        with_default: int = 5,
        falsy_default: bool = False,
        empty_default: str = "",
        marked: str = Query("q", description="a marked param"),
    ) -> dict:
        return {
            "required": required,
            "legacy_optional": legacy_optional,
            "union_optional": union_optional,
            "with_default": with_default,
            "falsy_default": falsy_default,
            "empty_default": empty_default,
            "marked": marked,
        }

    return app


def _props() -> dict:
    return build_registry(_app()).tools["probe"].input_schema["properties"]


async def _call(arguments: dict) -> dict:
    response = await MCPServer(_app()).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "probe", "arguments": arguments},
        },
        MCPSession(),
    )
    result = response["result"]
    assert not result.get("isError"), result["content"][0]["text"]
    return orjson.loads(result["content"][0]["text"])


# ── The null branch ──────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["legacy_optional", "union_optional"])
def test_an_optional_parameter_publishes_a_null_branch(name: str):
    """The regression this guards: the published type rejected an explicit null."""
    assert _props()[name] == {"anyOf": [{"type": "string"}, {"type": "null"}]}


def test_a_required_parameter_gets_no_null_branch():
    assert _props()["required"] == {"type": "integer"}


def test_a_defaulted_parameter_is_not_widened_to_null():
    """A default makes a field optional to *send*, not nullable to *value*."""
    assert "anyOf" not in _props()["with_default"]


# ── Declared defaults ────────────────────────────────────────────────


def test_a_bare_default_is_published():
    assert _props()["with_default"] == {"type": "integer", "default": 5}


@pytest.mark.parametrize(
    ("name", "expected"),
    [("falsy_default", False), ("empty_default", "")],
)
def test_a_falsy_default_is_published_too(name: str, expected):
    """`False` and `""` are values a client should send, not absent defaults."""
    assert _props()[name]["default"] == expected


def test_a_none_default_is_carried_by_the_null_branch_not_a_default_key():
    assert "default" not in _props()["legacy_optional"]


def test_a_marker_default_still_wins():
    marked = _props()["marked"]
    assert marked["default"] == "q"
    assert marked["description"] == "a marked param"


def test_only_a_parameter_without_a_default_is_required():
    schema = build_registry(_app()).tools["probe"].input_schema
    assert schema["required"] == ["required"]


# ── The published contract is the one the call path honours ──────────


async def test_an_explicit_null_is_accepted_as_the_schema_now_says():
    payload = await _call({"required": 1, "legacy_optional": None})
    assert payload["legacy_optional"] is None


async def test_omitting_an_optional_parameter_still_works():
    payload = await _call({"required": 1})
    assert payload["legacy_optional"] is None
    assert payload["union_optional"] is None


async def test_the_published_default_is_the_value_the_server_falls_back_to():
    payload = await _call({"required": 1})
    assert payload["with_default"] == _props()["with_default"]["default"]
    assert payload["falsy_default"] == _props()["falsy_default"]["default"]


async def test_sending_the_published_default_explicitly_is_accepted():
    payload = await _call({"required": 1, "with_default": 5})
    assert payload["with_default"] == 5


async def test_a_supplied_value_still_overrides_the_default():
    payload = await _call({"required": 1, "with_default": 9, "legacy_optional": "set"})
    assert payload["with_default"] == 9
    assert payload["legacy_optional"] == "set"


# ── What a list of models publishes ──────────────────────────────────


class _Step(BaseModel):
    tool: str
    quiet: bool = False


def _list_app() -> Veloce:
    app = Veloce(title="Listed", openapi_url=None)

    @app.mcp_tool(description="Run several steps")
    async def run(steps: list[_Step], labels: list[str] | None = None) -> dict:
        return {
            "types": [type(step).__name__ for step in steps],
            "tools": [step.tool for step in steps],
            "labels": labels,
        }

    return app


def test_a_list_of_models_publishes_the_model_as_its_item():
    """Over HTTP such a value arrives as text; an MCP argument is JSON."""
    schema = build_registry(_list_app()).tools["run"].input_schema
    assert schema["properties"]["steps"]["items"] == {"$ref": "#/$defs/_Step"}


def test_the_referenced_model_is_defined_in_the_schema():
    schema = build_registry(_list_app()).tools["run"].input_schema
    assert set(schema["$defs"]["_Step"]["properties"]) == {"tool", "quiet"}


def test_a_list_of_scalars_is_unchanged():
    schema = build_registry(_list_app()).tools["run"].input_schema
    listed = schema["properties"]["labels"]["anyOf"][0]
    assert listed == {"type": "array", "items": {"type": "string"}}


async def test_the_published_shape_is_the_one_the_handler_takes():
    response = await MCPServer(_list_app()).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "run",
                "arguments": {"steps": [{"tool": "a"}, {"tool": "b", "quiet": True}]},
            },
        },
        MCPSession(),
    )
    payload = orjson.loads(response["result"]["content"][0]["text"])
    assert payload["types"] == ["_Step", "_Step"]
    assert payload["tools"] == ["a", "b"]
