"""A tool listing entry matches the shape its revision defines.

The handshake revisions define `execution` on `Tool`; the modern revision
removed it, because task support is negotiated through the extension capability
instead. The entry was emitted era-blind, so a modern client received a listing
that does not validate against the schema it negotiated.

Each shape is memoized separately, so serving both costs no more per listing
than serving one did - and a tool whose shape does not differ between revisions
allocates only the one entry.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.tasks import TASKS_EXTENSION

_MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}},
}


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Runs in the background", task_support=True)
    async def slow() -> dict:
        return {"ok": True}

    @app.mcp_tool(description="Runs inline")
    async def plain() -> dict:
        return {"ok": True}

    return app


async def _entries(server: MCPServer, *, modern: bool) -> dict[str, dict]:
    params: dict = {"_meta": _MODERN_META} if modern else {}
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params}, MCPSession()
    )
    return {tool["name"]: tool for tool in response["result"]["tools"]}


async def test_the_modern_revision_omits_execution():
    """The defect: it was emitted, and does not validate there."""
    entries = await _entries(MCPServer(_app()), modern=True)
    assert "execution" not in entries["slow"]


async def test_a_handshake_revision_still_carries_execution():
    entries = await _entries(MCPServer(_app()), modern=False)
    assert entries["slow"]["execution"] == {"taskSupport": "optional"}


@pytest.mark.parametrize("modern", [True, False])
async def test_a_tool_without_task_support_never_carries_it(modern: bool):
    """The field's absence is the spec default, on either revision."""
    entries = await _entries(MCPServer(_app()), modern=modern)
    assert "execution" not in entries["plain"]


@pytest.mark.parametrize("modern", [True, False])
async def test_everything_else_about_the_entry_is_unchanged(modern: bool):
    entries = await _entries(MCPServer(_app()), modern=modern)
    entry = entries["slow"]
    assert entry["name"] == "slow"
    assert entry["description"] == "Runs in the background"
    assert "inputSchema" in entry


async def test_the_two_shapes_are_memoized_separately():
    """Serving both revisions must not rebuild an entry per listing."""
    server = MCPServer(_app())
    await _entries(server, modern=False)
    await _entries(server, modern=True)
    tool = server.registry.tools["slow"]
    assert "execution" in tool.listing_entry
    assert "execution" not in tool.listing_entry_modern
    assert tool.listing_entry is not tool.listing_entry_modern


async def test_a_repeated_listing_reuses_the_memo():
    server = MCPServer(_app())
    first = await _entries(server, modern=True)
    tool = server.registry.tools["slow"]
    memo = tool.listing_entry_modern
    second = await _entries(server, modern=True)
    assert tool.listing_entry_modern is memo
    assert first["slow"] == second["slow"]


async def test_a_revision_independent_tool_shares_one_entry():
    """A tool with nothing revision-specific should not allocate a second dict."""
    server = MCPServer(_app())
    await _entries(server, modern=True)
    plain = server.registry.tools["plain"]
    assert plain.listing_entry_modern is plain.listing_entry


async def test_the_modern_listing_still_names_every_tool():
    entries = await _entries(MCPServer(_app()), modern=True)
    assert set(entries) == {"slow", "plain"}
