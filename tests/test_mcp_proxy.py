"""Serving another MCP server's tools as if they were this app's own.

A gateway fronting several MCP servers needs their catalogues in its own
`tools/list` and its calls forwarded. Nothing could express that: the registry
builders require a Veloce app to walk, so a server this app does not own was
unreachable even by hand.

The connection stays with the application — `add_mcp_proxy` takes a callable that
performs one JSON-RPC request — so retries, credentials and timeouts belong to
whoever knows the deployment.
"""

from __future__ import annotations

import pytest

from tests._mcp import FORBIDDEN, call_tool
from veloce import Principal, Veloce
from veloce.contrib.mcp.proxy import add_mcp_proxy
from veloce.contrib.mcp.registry import build_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.principal import set_principal


def _upstream() -> tuple[Veloce, MCPServer]:
    app = Veloce(title="Upstream", version="1.0.0", openapi_url=None)

    @app.mcp_tool(
        description="Add two numbers",
        annotations={"readOnlyHint": True},
        meta={"io.example/team": "math"},
    )
    async def add(a: int, b: int) -> int:
        return a + b

    @app.mcp_tool(description="Always fails")
    async def boom() -> int:
        raise ValueError("upstream failed")

    return app, MCPServer(app)


def _requester(server: MCPServer, seen: list | None = None):
    async def request(method: str, params: dict) -> dict:
        if seen is not None:
            seen.append((method, params))
        response = await server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, MCPSession()
        )
        return response["result"]

    return request


async def _gateway(namespace: str = "up", seen: list | None = None) -> Veloce:
    _app, server = _upstream()
    gateway = Veloce(title="Gateway", version="1.0.0", openapi_url=None)
    await add_mcp_proxy(gateway, namespace, _requester(server, seen))
    return gateway


# ── Discovery ────────────────────────────────────────────────────────


async def test_the_upstream_catalogue_is_registered_under_the_namespace():
    gateway = await _gateway()
    assert sorted(build_registry(gateway).tools) == ["up_add", "up_boom"]


async def test_the_registered_names_are_returned():
    _app, server = _upstream()
    gateway = Veloce(title="G", openapi_url=None)
    assert await add_mcp_proxy(gateway, "up", _requester(server)) == ["up_add", "up_boom"]


async def test_an_empty_namespace_keeps_the_upstream_names():
    gateway = await _gateway(namespace="")
    assert sorted(build_registry(gateway).tools) == ["add", "boom"]


async def test_the_upstream_schema_is_published_verbatim():
    """It is what the upstream validates against; rebuilding it could only differ."""
    tool = build_registry(await _gateway()).tools["up_add"]
    assert tool.input_schema["properties"] == {
        "a": {"type": "integer"},
        "b": {"type": "integer"},
    }
    assert sorted(tool.input_schema["required"]) == ["a", "b"]


async def test_the_upstream_description_and_metadata_travel():
    tool = build_registry(await _gateway()).tools["up_add"]
    assert tool.description == "Add two numbers"
    assert tool.annotations == {"readOnlyHint": True}
    assert tool.meta == {"io.example/team": "math"}


async def test_discovery_follows_the_upstream_cursor():
    """An upstream that paginates must still be discovered whole."""
    app = Veloce(title="Big", openapi_url=None)
    for index in range(5):

        def handler(_i: int = index) -> int:
            return _i

        handler.__name__ = f"tool_{index}"
        app.mcp_tool(description=f"Tool {index}")(handler)

    server = MCPServer(app, page_size=2)
    gateway = Veloce(title="G", openapi_url=None)
    names = await add_mcp_proxy(gateway, "up", _requester(server))
    assert len(names) == 5


async def test_an_upstream_that_never_stops_paginating_is_refused():
    async def endless(method: str, params: dict) -> dict:
        return {"tools": [{"name": "t", "inputSchema": {}}], "nextCursor": "more"}

    with pytest.raises(RuntimeError, match="pagination cursor"):
        await add_mcp_proxy(Veloce(title="G", openapi_url=None), "up", endless)


async def test_a_listing_entry_without_a_name_is_skipped():
    async def odd(method: str, params: dict) -> dict:
        return {"tools": [{"description": "nameless"}, {"name": "real", "inputSchema": {}}]}

    gateway = Veloce(title="G", openapi_url=None)
    assert await add_mcp_proxy(gateway, "up", odd) == ["up_real"]


# ── Forwarding ───────────────────────────────────────────────────────


async def test_a_call_is_forwarded_and_its_answer_relayed():
    assert (await call_tool(await _gateway(), "up_add", {"a": 2, "b": 3}))["content"][0][
        "text"
    ] == "5"


async def test_the_upstream_is_asked_for_the_name_it_knows():
    """The namespace is local; the upstream never hears it."""
    seen: list = []
    gateway = await _gateway(seen=seen)
    seen.clear()
    await call_tool(gateway, "up_add", {"a": 1, "b": 1})
    method, params = seen[0]
    assert method == "tools/call"
    assert params["name"] == "add"
    assert params["arguments"] == {"a": 1, "b": 1}


async def test_an_upstream_failure_is_relayed_as_a_failure():
    """Re-shaping would bury the upstream's own `isError` inside a text block."""
    result = await call_tool(await _gateway(), "up_boom")
    assert result["isError"] is True


async def test_the_relayed_result_is_not_nested():
    result = await call_tool(await _gateway(), "up_add", {"a": 2, "b": 2})
    assert result["content"][0]["text"] == "4"
    assert "content" not in result["content"][0]["text"]


# ── Alongside local tools ────────────────────────────────────────────


async def test_local_and_proxied_tools_are_served_together():
    _app, server = _upstream()
    gateway = Veloce(title="Gateway", openapi_url=None)

    @gateway.mcp_tool(description="Something local")
    async def local() -> int:
        return 0

    await add_mcp_proxy(gateway, "up", _requester(server))
    assert sorted(build_registry(gateway).tools) == ["local", "up_add", "up_boom"]


async def test_two_upstreams_stay_distinct():
    _one, first = _upstream()
    _two, second = _upstream()
    gateway = Veloce(title="Gateway", openapi_url=None)
    await add_mcp_proxy(gateway, "alpha", _requester(first))
    await add_mcp_proxy(gateway, "beta", _requester(second))
    assert sorted(build_registry(gateway).tools) == [
        "alpha_add",
        "alpha_boom",
        "beta_add",
        "beta_boom",
    ]


async def test_a_proxied_tool_is_listed_like_any_other():
    response = await MCPServer(await _gateway()).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, MCPSession()
    )
    entry = next(t for t in response["result"]["tools"] if t["name"] == "up_add")
    assert entry["description"] == "Add two numbers"
    assert entry["annotations"] == {"readOnlyHint": True}
    assert entry["_meta"] == {"io.example/team": "math"}


# ── A local tool is unaffected ───────────────────────────────────────


async def test_a_local_tool_result_is_still_shaped_normally():
    """Passthrough belongs to proxied tools only."""
    gateway = Veloce(title="Gateway", openapi_url=None)

    @gateway.mcp_tool(description="Returns a result-shaped dict")
    async def looks_like_a_result() -> dict:
        return {"content": [{"type": "text", "text": "not a relay"}]}

    result = await call_tool(gateway, "looks_like_a_result")
    # Shaped, not relayed: the dict is serialised into the text block.
    assert result["content"][0]["text"].startswith("{")


# ── What the gateway forwards, and what it can require ───────────────


async def test_the_callers_meta_travels_upstream():
    """A progress token or a trace id the caller attached is the upstream's too."""
    seen: list = []
    gateway = await _gateway(seen=seen)
    seen.clear()
    await MCPServer(gateway).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "up_add",
                "arguments": {"a": 1, "b": 1},
                "_meta": {"progressToken": "tok-1", "io.example/trace": "abc"},
            },
        },
        MCPSession(),
    )
    _method, params = seen[0]
    assert params["_meta"] == {"progressToken": "tok-1", "io.example/trace": "abc"}


async def test_a_call_without_meta_forwards_none():
    """Nothing invented: an absent `_meta` stays absent upstream."""
    seen: list = []
    gateway = await _gateway(seen=seen)
    seen.clear()
    await call_tool(gateway, "up_add", {"a": 1, "b": 1})
    _method, params = seen[0]
    assert "_meta" not in params


async def test_a_proxied_tool_can_require_a_scope():
    """A gateway is where authorization matters; the upstream cannot see the caller."""
    _app, server = _upstream()
    gateway = Veloce(title="Gateway", openapi_url=None)
    await add_mcp_proxy(gateway, "up", _requester(server), scopes=["ops"])
    assert build_registry(gateway).tools["up_add"].required_scopes == frozenset({"ops"})


async def test_a_caller_without_the_scope_is_refused():

    _app, server = _upstream()
    gateway = Veloce(title="Gateway", openapi_url=None)
    await add_mcp_proxy(gateway, "up", _requester(server), scopes=["ops"])
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    response = await MCPServer(gateway).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "up_add", "arguments": {"a": 1, "b": 1}},
        },
        MCPSession(),
    )
    assert response["error"]["code"] == FORBIDDEN


async def test_a_proxied_tool_can_be_tagged_for_a_visibility_policy():
    _app, server = _upstream()
    gateway = Veloce(title="Gateway", openapi_url=None)
    await add_mcp_proxy(gateway, "up", _requester(server), tags=["upstream", "beta"])
    assert build_registry(gateway).tools["up_add"].tags == frozenset({"upstream", "beta"})


async def test_scopes_and_tags_default_to_nothing():
    tool = build_registry(await _gateway()).tools["up_add"]
    assert tool.required_scopes == frozenset()
    assert tool.tags == frozenset()
