"""Narrowing one connection's view of the catalogue, from inside a call.

`mount_mcp(tool_filter=...)` is a policy fixed when the server is mounted. What
was missing is a running call changing what *its own* client sees — unlocking a
tool once a licence is verified, hiding a step once it is done — without touching
what any other client is served.

For a tool, hiding is enforcement too, exactly as with `tool_filter`: a hidden
tool is refused at `tools/call` the same way an unregistered one would be. A
hidden prompt or resource is not yet refused the same way at
`prompts/get`/`resources/read` - only its listing narrows.
"""

from __future__ import annotations

import pytest

from tests._mcp import INVALID_PARAMS
from veloce import MCPContext, Veloce
from veloce.contrib.mcp._helpers import _notifier_var
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


def _app() -> Veloce:
    app = Veloce(title="Visible", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Hide the secret")
    async def lock(ctx: MCPContext) -> str:
        await ctx.hide("secret_tool")
        return "hidden"

    @app.mcp_tool(description="Show it again")
    async def unlock(ctx: MCPContext) -> str:
        await ctx.unhide("secret_tool")
        return "shown"

    @app.mcp_tool(description="Show everything again")
    async def reset(ctx: MCPContext) -> str:
        await ctx.reset_visibility()
        return "reset"

    @app.mcp_tool(description="A secret tool")
    async def secret_tool() -> int:
        return 42

    @app.mcp_prompt(description="A secret prompt")
    async def secret_prompt() -> str:
        return "shh"

    @app.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="doc://secret",
        mcp_description="A secret document",
    )
    async def doc() -> dict:
        return {}

    return app


async def _names(server: MCPServer, session: MCPSession, method: str, key: str) -> list[str]:
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}, session
    )
    return sorted(
        entry.get("name") if key != "resources" else entry["uri"]
        for entry in response["result"][key]
    )


async def _call(
    server: MCPServer, session: MCPSession, name: str, notes: list | None = None
) -> dict:
    async def sink(message: dict) -> None:
        if notes is not None:
            notes.append(message["method"])

    token = _notifier_var.set(sink)
    try:
        response = await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": {}},
            },
            session,
        )
    finally:
        _notifier_var.reset(token)
    return response["result"]


async def _call_error(server: MCPServer, session: MCPSession, name: str) -> dict:
    """Same dispatch as `_call`, for a call expected to fail."""
    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        },
        session,
    )
    assert "error" in response, f"tools/call unexpectedly succeeded: {response}"
    return response["error"]


# ── One connection's view ────────────────────────────────────────────


async def test_a_call_can_hide_a_tool_from_its_own_client():
    server, session = MCPServer(_app()), MCPSession()
    assert "secret_tool" in await _names(server, session, "tools/list", "tools")
    await _call(server, session, "lock")
    assert "secret_tool" not in await _names(server, session, "tools/list", "tools")


async def test_hiding_leaves_every_other_tool_listed():
    server, session = MCPServer(_app()), MCPSession()
    await _call(server, session, "lock")
    assert await _names(server, session, "tools/list", "tools") == ["lock", "reset", "unlock"]


async def test_unhiding_restores_it():
    server, session = MCPServer(_app()), MCPSession()
    await _call(server, session, "lock")
    await _call(server, session, "unlock")
    assert "secret_tool" in await _names(server, session, "tools/list", "tools")


async def test_resetting_restores_everything():
    server, session = MCPServer(_app()), MCPSession()
    await _call(server, session, "lock")
    await _call(server, session, "reset")
    assert "secret_tool" in await _names(server, session, "tools/list", "tools")


# ── Other connections are untouched ──────────────────────────────────


async def test_another_connection_still_sees_it():
    """The whole point: this is one client's view, not a server-wide change."""
    server = MCPServer(_app())
    mine, theirs = MCPSession(), MCPSession()
    await _call(server, mine, "lock")
    assert "secret_tool" not in await _names(server, mine, "tools/list", "tools")
    assert "secret_tool" in await _names(server, theirs, "tools/list", "tools")


# ── The client is told ───────────────────────────────────────────────


async def test_the_connection_is_told_its_lists_changed():
    server, session = MCPServer(_app()), MCPSession()
    notes: list = []
    await _call(server, session, "lock", notes)
    assert "notifications/tools/list_changed" in notes


async def test_a_change_that_changes_nothing_says_nothing():
    """Hiding what is already hidden must not spam the client."""
    server, session = MCPServer(_app()), MCPSession()
    await _call(server, session, "lock")
    notes: list = []
    await _call(server, session, "lock", notes)
    assert notes == []


# ── Prompts and resources too ────────────────────────────────────────


async def test_a_prompt_can_be_hidden():
    app = _app()

    @app.mcp_tool(description="Hide the prompt")
    async def lock_prompt(ctx: MCPContext) -> str:
        await ctx.hide("secret_prompt")
        return "ok"

    server, session = MCPServer(app), MCPSession()
    await _call(server, session, "lock_prompt")
    assert "secret_prompt" not in await _names(server, session, "prompts/list", "prompts")


async def test_a_resource_is_hidden_by_its_uri():
    app = _app()

    @app.mcp_tool(description="Hide the document")
    async def lock_doc(ctx: MCPContext) -> str:
        await ctx.hide("doc://secret")
        return "ok"

    server, session = MCPServer(app), MCPSession()
    assert "doc://secret" in await _names(server, session, "resources/list", "resources")
    await _call(server, session, "lock_doc")
    assert "doc://secret" not in await _names(server, session, "resources/list", "resources")


# ── Hiding is enforced at call time too ───────────────────────────────


async def test_a_hidden_tool_is_rejected_as_unknown():
    """Same contract as `tool_filter`: a name this connection hid is no
    longer callable either - not just omitted from its own tools/list."""
    server, session = MCPServer(_app()), MCPSession()
    await _call(server, session, "lock")
    error = await _call_error(server, session, "secret_tool")
    assert error["code"] == INVALID_PARAMS
    assert "secret_tool" in error["message"]


async def test_unhiding_restores_callability_too():
    server, session = MCPServer(_app()), MCPSession()
    await _call(server, session, "lock")
    await _call(server, session, "unlock")
    assert (await _call(server, session, "secret_tool"))["content"][0]["text"] == "42"


async def test_hide_and_tool_filter_together_reject_once_cleanly():
    """Review Focus #7: a tool hidden by BOTH this connection's `hide()` and
    the server's `tool_filter` must still produce exactly one clean
    rejection, not a double-evaluation error or a conflicting result."""
    app = _app()
    server = MCPServer(app, tool_filter=lambda tool, principal: tool.name != "secret_tool")
    session = MCPSession()
    await _call(server, session, "lock")
    error = await _call_error(server, session, "secret_tool")
    assert error["code"] == INVALID_PARAMS


# ── Paging sees the narrowed catalogue ───────────────────────────────


async def test_a_hidden_entry_does_not_occupy_a_page_slot():
    server, session = MCPServer(_app(), page_size=2), MCPSession()
    await _call(server, session, "lock")
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, session
    )
    listed = [entry["name"] for entry in response["result"]["tools"]]
    assert "secret_tool" not in listed
    assert len(listed) == 2


# ── Off a stateful connection ────────────────────────────────────────


async def test_hiding_needs_a_connection_to_belong_to():
    """The stateless HTTP path has no connection whose view could be narrowed."""
    context = MCPContext("bare")
    with pytest.raises(RuntimeError, match="property of a connection"):
        await context.hide("anything")
