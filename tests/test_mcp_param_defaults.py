"""A parameter marker's default reaches an MCP tool call and its input schema."""

from __future__ import annotations

from typing import Annotated

import orjson

from veloce import Body, Query, Veloce
from veloce.contrib.mcp.server import MCPServer


def _tool_app() -> Veloce:
    app = Veloce(title="probe")

    @app.post("/api/probe", expose_as_mcp_tool=True, mcp_description="probe")
    async def probe(
        max_messages: Annotated[int, Body(500, embed=True)] = 500,
        flag: Annotated[bool, Body(False, embed=True)] = False,
    ) -> dict:
        return {"max_messages": max_messages, "flag": flag}

    return app


async def _call(server: MCPServer, name: str, arguments: dict) -> dict:
    reply = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert "error" not in reply, reply.get("error")
    return orjson.loads(reply["result"]["content"][0]["text"])


async def _tools(server: MCPServer) -> dict:
    listed = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    return listed["result"]["tools"][0]


async def test_omitted_argument_uses_the_marker_default():
    # An omitted field must resolve to the declared default, not None - a None
    # reaching an int parameter fails the handler on its first arithmetic.
    payload = await _call(MCPServer(_tool_app()), "probe", {})
    assert payload == {"max_messages": 500, "flag": False}


async def test_supplied_argument_still_wins():
    payload = await _call(MCPServer(_tool_app()), "probe", {"max_messages": 10})
    assert payload["max_messages"] == 10


async def test_input_schema_advertises_the_default():
    props = (await _tools(MCPServer(_tool_app())))["inputSchema"]["properties"]
    assert props["max_messages"]["default"] == 500
    assert props["flag"]["default"] is False


async def test_query_marker_default_also_resolves():
    # The resolution is marker-generic, not Body-specific.
    app = Veloce(title="probe")

    @app.get("/api/q", expose_as_mcp_tool=True, mcp_description="q")
    async def q(limit: Annotated[int, Query(25)] = 25) -> dict:
        return {"limit": limit}

    assert (await _call(MCPServer(app), "q", {}))["limit"] == 25


async def test_default_factory_applies_but_is_not_advertised():
    # A factory builds a per-call value, so there is no single default to
    # publish - but the call must still receive a fresh one rather than None.
    app = Veloce(title="probe")

    @app.post("/api/f", expose_as_mcp_tool=True, mcp_description="f")
    async def f(tags: Annotated[list, Body(default_factory=list, embed=True)]) -> dict:
        return {"tags": tags}

    server = MCPServer(app)
    assert "default" not in (await _tools(server))["inputSchema"]["properties"]["tags"]
    assert (await _call(server, "f", {}))["tags"] == []


async def test_marker_without_a_default_stays_required():
    app = Veloce(title="probe")

    @app.post("/api/r", expose_as_mcp_tool=True, mcp_description="r")
    async def r(name: Annotated[str, Body(embed=True)]) -> dict:
        return {"name": name}

    schema = (await _tools(MCPServer(app)))["inputSchema"]
    assert schema.get("required") == ["name"]
    assert "default" not in schema["properties"]["name"]
