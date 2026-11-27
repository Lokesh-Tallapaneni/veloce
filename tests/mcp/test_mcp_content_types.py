"""Non-text tool content: images, audio, icons, resource links, embedded resources.

Split out of `test_mcp.py`, which had grown to 5,730 lines and 271 tests
behind a one-line docstring while labelling its own split points in section
comments. This is one of those points.
"""

from __future__ import annotations

import base64

import orjson

from tests._mcp_shared import (
    _call,
    _list_prompts,
    _list_resources,
    _list_tools,
)
from veloce import (
    JSONResponse,
    Response,
    Veloce,
)
from veloce.contrib.mcp import Icon

# -- Non-text tool content (image / audio) ----------------------------


def test_pure_tool_image_response_emits_image_block():
    app = Veloce(openapi_url=None)
    png = b"\x89PNG\r\n\x1a\nfake-image-bytes"

    @app.mcp_tool(description="Render a chart")
    async def chart() -> Response:
        return Response(body=png, content_type="image/png")

    result = _call(app, "chart", {})["result"]
    block = result["content"][0]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    assert base64.b64decode(block["data"]) == png
    # An image body has no text form, so no decoded-text block is emitted.
    assert len(result["content"]) == 1


def test_route_tool_audio_response_emits_audio_block():
    app = Veloce(openapi_url=None)
    wav = b"RIFF....WAVEfake-audio"

    @app.get("/say", expose_as_mcp_tool=True, mcp_description="Synthesize speech")
    async def say() -> Response:
        return Response(body=wav, content_type="audio/wav")

    result = _call(app, "say", {})["result"]
    block = result["content"][0]
    assert block["type"] == "audio"
    assert block["mimeType"] == "audio/wav"
    assert base64.b64decode(block["data"]) == wav
    assert "structuredContent" not in result


def test_non_binary_response_still_emits_text_block():
    """A JSON/text response is unaffected by the non-text shaping path."""
    app = Veloce(openapi_url=None)

    @app.get("/data2", expose_as_mcp_tool=True, mcp_description="Raw data")
    async def data2() -> JSONResponse:
        return JSONResponse({"value": 42})

    result = _call(app, "data2", {})["result"]
    assert result["content"][0]["type"] == "text"
    assert orjson.loads(result["content"][0]["text"]) == {"value": 42}


# -- Icons + resource-link / embedded content -------------------------


def test_mcp_tool_icons_appear_in_tools_list():
    """An `@app.mcp_tool` icon set surfaces as the tool's `icons` array."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(
        description="Add two integers",
        icons=[Icon("https://x/add.png", mime_type="image/png", sizes=("48x48",))],
    )
    async def add(a: int, b: int) -> int:
        return a + b

    entry = _list_tools(app)["add"]
    assert entry["icons"] == [
        {"src": "https://x/add.png", "mimeType": "image/png", "sizes": ["48x48"]}
    ]


def test_route_tool_icons_appear_in_tools_list():
    """A route exposed as a tool carries its `mcp_icons` into tools/list."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/ping",
        expose_as_mcp_tool=True,
        mcp_description="Health probe",
        mcp_icons=[Icon("https://x/ping.svg")],
    )
    async def ping():
        return {"pong": True}

    assert _list_tools(app)["ping"]["icons"] == [{"src": "https://x/ping.svg"}]


def test_tool_without_icons_emits_no_icons_key():
    """A tool with no icons omits the `icons` key entirely (unchanged wire form)."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Add two integers")
    async def add(a: int, b: int) -> int:
        return a + b

    assert "icons" not in _list_tools(app)["add"]


def test_resource_icons_appear_in_resources_list():
    """A route exposed as a resource carries its `mcp_icons` into resources/list."""
    app = Veloce(openapi_url=None)

    @app.get(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_description="Settings",
        mcp_resource_uri="config://app",
        mcp_icons=[Icon("https://x/cfg.png")],
    )
    async def settings():
        return {"debug": False}

    assert _list_resources(app)["config://app"]["icons"] == [{"src": "https://x/cfg.png"}]


def test_prompt_icons_appear_in_prompts_list():
    """An `@app.mcp_prompt` icon set surfaces as the prompt's `icons` array."""
    app = Veloce(openapi_url=None)

    @app.mcp_prompt(description="Summarise a topic", icons=[Icon("https://x/p.png")])
    async def summarise(topic: str) -> str:
        return f"Summarise {topic}."

    assert _list_prompts(app)["summarise"]["icons"] == [{"src": "https://x/p.png"}]


def test_route_result_emits_resource_link_block():
    """A route setting the resource-link header returns a `resource_link` block."""
    app = Veloce(openapi_url=None)

    @app.get("/doc", expose_as_mcp_tool=True, mcp_description="Doc pointer")
    async def doc() -> Response:
        return Response(
            body=b"see resource",
            content_type="text/plain",
            headers={"X-MCP-Resource-Link": "res://doc/1"},
        )

    block = _call(app, "doc", {})["result"]["content"][0]
    assert block["type"] == "resource_link"
    assert block["uri"] == "res://doc/1"
    assert block["name"] == "doc"


def test_route_result_emits_embedded_resource_block():
    """A route setting the embedded header inlines its body as a `resource` block."""
    app = Veloce(openapi_url=None)

    @app.get("/inline", expose_as_mcp_tool=True, mcp_description="Inline data")
    async def inline() -> Response:
        return Response(
            body=b"hello",
            content_type="text/plain",
            headers={"X-MCP-Embedded-Resource": "res://inline/1"},
        )

    block = _call(app, "inline", {})["result"]["content"][0]
    assert block["type"] == "resource"
    assert block["resource"] == {"uri": "res://inline/1", "mimeType": "text/plain", "text": "hello"}


def test_route_result_without_resource_header_stays_text():
    """A route with neither resource header keeps the plain text result shape."""
    app = Veloce(openapi_url=None)

    @app.get("/plain", expose_as_mcp_tool=True, mcp_description="Plain")
    async def plain() -> Response:
        return Response(body=b"hi", content_type="text/plain")

    block = _call(app, "plain", {})["result"]["content"][0]
    assert block == {"type": "text", "text": "hi"}
