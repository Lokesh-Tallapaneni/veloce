"""A tool result carrying more than one content block.

`content` is an array, so a handler with more than one thing to say - a caption
beside an image, a chart beside the figures behind it - returns the blocks it
wants and they are emitted in order. Returning a block used to serialise the
object's repr into a single text block, which is what a client saw.

Any other return is data and shapes exactly as it did, so a tool returning a
`dict`, a scalar, or a plain list is unaffected.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from veloce import Veloce
from veloce.contrib.mcp import (
    AudioContent,
    ContentBlock,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
)
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


class Row(BaseModel):
    n: int


PNG = "iVBORw0KGgo="


async def _call(app: Veloce, name: str, arguments: dict | None = None) -> dict:
    response = await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        MCPSession(),
    )
    assert "error" not in response, response
    return response["result"]


# ── More than one block ──────────────────────────────────────────────


async def test_a_list_of_blocks_is_emitted_in_order():
    app = Veloce(title="Blocks", openapi_url=None)

    @app.mcp_tool(description="A caption beside a figure")
    async def chart():
        return [TextContent("Revenue, Q3"), ImageContent(PNG, "image/png")]

    result = await _call(app, "chart")
    assert result["content"] == [
        {"type": "text", "text": "Revenue, Q3"},
        {"type": "image", "data": PNG, "mimeType": "image/png"},
    ]


async def test_a_single_block_needs_no_list():
    app = Veloce(title="Blocks2", openapi_url=None)

    @app.mcp_tool(description="One block")
    async def one():
        return TextContent("just text")

    assert (await _call(app, "one"))["content"] == [{"type": "text", "text": "just text"}]


async def test_a_tuple_of_blocks_works_too():
    app = Veloce(title="Blocks3", openapi_url=None)

    @app.mcp_tool(description="A tuple")
    async def pair():
        return (TextContent("a"), TextContent("b"))

    assert [b["text"] for b in (await _call(app, "pair"))["content"]] == ["a", "b"]


async def test_every_block_kind_renders():
    app = Veloce(title="Blocks4", openapi_url=None)

    @app.mcp_tool(description="One of each")
    async def every():
        return [
            TextContent("text"),
            ImageContent(PNG, "image/png"),
            AudioContent("YXVkaW8=", "audio/wav"),
            ResourceLink("file://report.csv", "report"),
            EmbeddedResource({"uri": "file://n.txt", "text": "inline", "mimeType": "text/plain"}),
        ]

    kinds = [block["type"] for block in (await _call(app, "every"))["content"]]
    assert kinds == ["text", "image", "audio", "resource_link", "resource"]


async def test_a_block_keeps_its_annotations():
    app = Veloce(title="Blocks5", openapi_url=None)

    @app.mcp_tool(description="Annotated")
    async def annotated():
        return [TextContent("for the user", annotations={"audience": ["user"], "priority": 0.9})]

    block = (await _call(app, "annotated"))["content"][0]
    assert block["annotations"] == {"audience": ["user"], "priority": 0.9}


async def test_a_block_result_is_not_an_error():
    app = Veloce(title="Blocks6", openapi_url=None)

    @app.mcp_tool(description="Fine")
    async def fine():
        return [TextContent("ok")]

    assert "isError" not in await _call(app, "fine")


async def test_a_user_defined_block_subclass_renders():
    """The family is extended by subclassing, not by hand-writing a dict."""

    class Diagnostic(ContentBlock):
        __slots__ = ("code",)

        def __init__(self, code: str) -> None:
            super().__init__()
            self.code = code

        def _body(self) -> dict:
            return {"type": "text", "text": f"diagnostic {self.code}"}

    app = Veloce(title="Blocks7", openapi_url=None)

    @app.mcp_tool(description="Custom block")
    async def custom():
        return [Diagnostic("E401")]

    assert (await _call(app, "custom"))["content"] == [{"type": "text", "text": "diagnostic E401"}]


# ── A mixed list is refused, not silently mangled ────────────────────


async def test_a_list_mixing_blocks_and_data_is_reported():
    app = Veloce(title="Mixed", openapi_url=None)

    @app.mcp_tool(description="Half blocks")
    async def half():
        return [TextContent("a"), {"not": "a block"}]

    result = await _call(app, "half")
    assert result["isError"] is True
    message = result["content"][0]["text"]
    assert "item 1" in message and "dict" in message


async def test_the_message_names_the_offending_position():
    app = Veloce(title="Mixed2", openapi_url=None)

    @app.mcp_tool(description="Third is wrong")
    async def third():
        return [TextContent("a"), TextContent("b"), 42]

    assert "item 2" in (await _call(app, "third"))["content"][0]["text"]


# ── Data returns are untouched ───────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [([1, 2, 3], "[1,2,3]"), ([], "[]"), (["a", "b"], '["a","b"]')],
)
async def test_a_plain_list_return_is_still_data(value, expected):
    app = Veloce(title="Data", openapi_url=None)

    @app.mcp_tool(description="Plain data")
    async def data():
        return value

    result = await _call(app, "data")
    assert result["content"] == [{"type": "text", "text": expected}]


async def test_a_mapping_return_is_still_data():
    app = Veloce(title="Data2", openapi_url=None)

    @app.mcp_tool(description="A mapping")
    async def mapping() -> dict:
        return {"rows": 2}

    assert (await _call(app, "mapping"))["content"][0]["text"] == '{"rows":2}'


async def test_a_scalar_return_is_still_data():
    app = Veloce(title="Data3", openapi_url=None)

    @app.mcp_tool(description="A scalar")
    async def scalar() -> int:
        return 7

    assert (await _call(app, "scalar"))["content"][0]["text"] == "7"


async def test_a_declared_output_schema_still_governs_a_data_return():
    """Blocks change the content array only; structured output is unaffected."""
    app = Veloce(title="Structured", openapi_url=None)

    @app.mcp_tool(description="Declares a shape")
    async def row() -> Row:
        return Row(n=3)

    assert (await _call(app, "row"))["structuredContent"] == {"n": 3}


async def test_blocks_carry_no_structured_content():
    """There is no object form to advertise, and none is invented."""
    app = Veloce(title="Structured2", openapi_url=None)

    @app.mcp_tool(description="Blocks only")
    async def blocks():
        return [TextContent("a")]

    assert "structuredContent" not in await _call(app, "blocks")
