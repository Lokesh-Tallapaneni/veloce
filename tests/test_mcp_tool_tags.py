"""Tags on a tool, and the format hint on a path parameter.

A visibility policy groups tools, but only a route-backed tool carried labels -
and only by reaching through `tool.route_info`, which is `None` for a tool
registered with `@app.mcp_tool`. Every tool now exposes `tags` directly, so one
policy reads both kinds the same way.

Tags stay server-side: the spec defines no tag field on a tool, so publishing
one would invent wire data a client cannot interpret.
"""

from __future__ import annotations

import pathlib

from veloce import APIRouter, Veloce
from veloce.contrib.mcp.registry import build_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


def _app() -> Veloce:
    app = Veloce(title="TagProbe", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="A labelled tool", tags=["math", "safe"])
    async def labelled(a: int) -> int:
        return a

    @app.mcp_tool(description="An unlabelled tool")
    async def unlabelled() -> int:
        return 1

    @app.get("/ops", expose_as_mcp_tool=True, mcp_description="Ops route", tags=["ops", "beta"])
    async def ops() -> dict:
        return {"ok": True}

    @app.get("/plain", expose_as_mcp_tool=True, mcp_description="Untagged route")
    async def plain() -> dict:
        return {"ok": True}

    return app


def _tools():
    return build_registry(_app()).tools


# ── Every tool exposes tags the same way ─────────────────────────────


def test_a_declared_tool_carries_its_tags():
    assert _tools()["labelled"].tags == frozenset({"math", "safe"})


def test_a_route_backed_tool_inherits_the_route_tags():
    assert _tools()["ops"].tags == frozenset({"ops", "beta"})


def test_a_tool_without_tags_carries_an_empty_set_not_none():
    """A policy should never need a `None` check to read tags."""
    for name in ("unlabelled", "plain"):
        assert _tools()[name].tags == frozenset()


def test_tags_are_a_frozenset_for_membership_tests():
    tags = _tools()["labelled"].tags
    assert isinstance(tags, frozenset)
    assert "math" in tags


def test_tags_survive_a_router_merge():
    router = APIRouter(prefix="/sub")

    @router.get("/x", expose_as_mcp_tool=True, mcp_description="Nested", tags=["nested"])
    async def x() -> dict:
        return {}

    app = Veloce(title="Merged", openapi_url=None)
    app.include_router(router)
    assert build_registry(app).tools["x"].tags == frozenset({"nested"})


# ── They are not wire data ───────────────────────────────────────────


async def test_tags_are_not_published_in_the_listing():
    """The spec defines no tag field on a tool; inventing one would mislead."""
    response = await MCPServer(_app()).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, MCPSession()
    )
    for entry in response["result"]["tools"]:
        assert "tags" not in entry


# ── One policy reads both kinds ──────────────────────────────────────


async def test_one_visibility_policy_filters_both_tool_kinds():
    """The point of the field: a filter no longer needs `tool.route_info`."""

    def hide_beta(tool, principal) -> bool:
        return "beta" not in tool.tags

    server = MCPServer(_app(), tool_filter=hide_beta)
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, MCPSession()
    )
    names = {t["name"] for t in response["result"]["tools"]}
    assert "ops" not in names
    assert {"labelled", "unlabelled", "plain"} <= names


async def test_a_policy_can_select_a_declared_tool_by_tag():
    def only_math(tool, principal) -> bool:
        return "math" in tool.tags

    server = MCPServer(_app(), tool_filter=only_math)
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, MCPSession()
    )
    assert {t["name"] for t in response["result"]["tools"]} == {"labelled"}


async def test_hiding_a_tool_by_tag_does_not_make_it_callable_by_others():
    """Hiding is not enforcement; the scope check is unaffected by tags."""

    def hide_all(tool, principal) -> bool:
        return False

    server = MCPServer(_app(), tool_filter=hide_all)
    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "labelled", "arguments": {"a": 3}},
        },
        MCPSession(),
    )
    assert "error" not in response


# ── A path parameter says what its string means ──────────────────────


def test_a_path_parameter_declares_the_path_format():
    app = Veloce(title="PathProbe", openapi_url=None)

    @app.mcp_tool(description="Takes a path")
    async def takes(p: pathlib.Path) -> dict:
        return {"name": p.name}

    prop = build_registry(app).tools["takes"].input_schema["properties"]["p"]
    assert prop == {"type": "string", "format": "path"}


async def test_a_path_parameter_still_arrives_as_a_path():
    app = Veloce(title="PathProbe2", openapi_url=None)

    @app.mcp_tool(description="Takes a path")
    async def takes(p: pathlib.Path) -> dict:
        return {"name": p.name, "is_path": isinstance(p, pathlib.PurePath)}

    response = await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "takes", "arguments": {"p": "docs/guide/mcp.md"}},
        },
        MCPSession(),
    )
    text = response["result"]["content"][0]["text"]
    assert '"is_path":true' in text
    assert '"name":"mcp.md"' in text
