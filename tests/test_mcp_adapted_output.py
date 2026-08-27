"""The output contract an object-shaped return declares.

A tool advertised an `outputSchema` only when its return annotation was a
Pydantic model, so a handler returning a dataclass or a `TypedDict` - shapes
accepted as *inputs* - published nothing about its result and returned no
`structuredContent`. Both now declare the same contract a model does.

A scalar or list return still declares none: `structuredContent` is a JSON
object per the spec, so there is nothing for a bare value to conform to, and
wrapping it in a synthetic envelope would invent a shape the spec does not
define.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import BaseModel, ValidationError
from typing_extensions import TypedDict

from veloce import Veloce
from veloce._model_backend import resolve_return_model, shape_through_model
from veloce.contrib.mcp.registry import build_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


@dataclass
class Report:
    title: str
    rows: int = 0


class Summary(TypedDict):
    total: int
    label: str


class Model(BaseModel):
    a: int


def _app() -> Veloce:
    app = Veloce(title="OutProbe", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Returns a dataclass")
    async def as_dataclass() -> Report:
        return Report(title="Q3", rows=12)

    @app.mcp_tool(description="Returns a TypedDict")
    async def as_typeddict() -> Summary:
        return {"total": 3, "label": "done"}

    @app.mcp_tool(description="Returns a model")
    async def as_model() -> Model:
        return Model(a=1)

    @app.mcp_tool(description="Returns a scalar")
    async def as_scalar() -> str:
        return "text"

    @app.mcp_tool(description="Returns a list")
    async def as_list() -> list[int]:
        return [1, 2]

    @app.mcp_tool(description="Returns a bare mapping")
    async def as_mapping() -> dict:
        return {"free": "form"}

    return app


def _tool(name: str) -> dict:
    """The `tools/list` entry for one tool, built without driving a loop."""
    tool = build_registry(_app()).tools[name]
    return MCPServer._describe_tool(tool)


async def _call(name: str) -> dict:
    response = await MCPServer(_app()).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        },
        MCPSession(),
    )
    result = response["result"]
    assert not result.get("isError"), result["content"][0]["text"]
    return result


# ── An object-shaped return declares a schema ────────────────────────


@pytest.mark.parametrize("name", ["as_dataclass", "as_typeddict", "as_model"])
def test_an_object_shaped_return_publishes_an_output_schema(name: str):
    assert _tool(name)["outputSchema"]["type"] == "object"


def test_the_schema_names_the_fields_and_which_are_required():
    schema = _tool("as_dataclass")["outputSchema"]
    assert sorted(schema["properties"]) == ["rows", "title"]
    assert schema["required"] == ["title"]


def test_a_typeddict_return_marks_every_key_required():
    schema = _tool("as_typeddict")["outputSchema"]
    assert sorted(schema["required"]) == ["label", "total"]


# ── The result carries structured content ────────────────────────────


@pytest.mark.parametrize("name", ["as_dataclass", "as_typeddict", "as_model"])
async def test_an_object_shaped_return_carries_structured_content(name: str):
    assert "structuredContent" in await _call(name)


async def test_the_structured_content_conforms_to_the_published_schema():
    result = await _call("as_dataclass")
    schema = _tool("as_dataclass")["outputSchema"]
    structured = result["structuredContent"]
    assert set(structured) == set(schema["properties"])
    assert structured == {"title": "Q3", "rows": 12}


async def test_a_typeddict_result_is_dumped_as_its_mapping():
    assert (await _call("as_typeddict"))["structuredContent"] == {"total": 3, "label": "done"}


async def test_the_text_block_is_still_present():
    """Structured content is additive; a client reading text keeps working."""
    result = await _call("as_dataclass")
    assert result["content"][0]["type"] == "text"


# ── A value with no object shape declares nothing ────────────────────


@pytest.mark.parametrize("name", ["as_scalar", "as_list", "as_mapping"])
def test_a_return_without_an_object_shape_declares_no_schema(name: str):
    assert "outputSchema" not in _tool(name)


@pytest.mark.parametrize("name", ["as_scalar", "as_list", "as_mapping"])
async def test_such_a_return_carries_no_structured_content(name: str):
    assert "structuredContent" not in await _call(name)


async def test_a_scalar_result_still_reaches_the_caller_as_text():
    assert (await _call("as_scalar"))["content"][0]["text"] == "text"


# ── The shared shaper ────────────────────────────────────────────────


def test_the_return_annotation_is_resolved_for_every_object_shape():
    async def dc_handler() -> Report: ...
    async def td_handler() -> Summary: ...
    async def scalar_handler() -> str: ...

    assert resolve_return_model(dc_handler) is Report
    assert resolve_return_model(td_handler) is Summary
    assert resolve_return_model(scalar_handler) is None


def test_the_shaper_coerces_onto_the_declared_shape():
    assert shape_through_model({"title": "Q", "rows": "4"}, Report) == {"title": "Q", "rows": 4}


def test_the_shaper_rejects_a_value_that_does_not_conform():
    with pytest.raises(ValidationError):
        shape_through_model({"rows": 1}, Report)


async def test_a_non_conforming_return_is_reported_not_emitted():
    """A result that cannot meet the advertised schema is an error, not a lie."""
    app = Veloce(title="Bad", openapi_url=None)

    @app.mcp_tool(description="Claims a shape it does not return")
    async def broken() -> Report:
        return {"rows": 1}  # type: ignore[return-value]

    response = await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "broken", "arguments": {}},
        },
        MCPSession(),
    )
    assert response["result"]["isError"] is True


# ── A route-backed tool declares the same contract ───────────────────


async def test_a_route_backed_tool_carries_structured_content_too():
    """The two doors build their result differently; both must honour the schema.

    A route-backed tool shapes its result from the HTTP response, a path that
    validated through `model_validate` and so degraded silently to text for a
    return shape that has no such method.
    """
    app = Veloce(title="RouteOut", openapi_url=None)

    @app.get("/report/{period}", expose_as_mcp_tool=True, mcp_description="Revenue")
    async def report(period: str) -> Report:
        return Report(title=period, rows=2)

    server = MCPServer(app)
    listed = MCPServer._describe_tool(server.registry.tools["report"])
    assert listed["outputSchema"]["type"] == "object"

    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "report", "arguments": {"period": "Q3"}},
        },
        MCPSession(),
    )
    result = response["result"]
    assert not result.get("isError"), result["content"][0]["text"]
    assert result["structuredContent"] == {"title": "Q3", "rows": 2}
