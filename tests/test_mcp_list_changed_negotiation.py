"""Telling a connection its listing changed, only where that was negotiated.

`MCPContext.hide` announced the change by sending all three `list_changed`
notifications, whatever the server exposed. A server with no prompts and no
resources advertises neither capability, so two of those three frames used
capabilities `initialize` never negotiated - which the lifecycle rules forbid
outright ("only use capabilities that were successfully negotiated").

Two things were wrong, and both had to change: the advertisement said the lists
could never change, which stopped being true the moment `hide` shipped, and the
announcement did not know which list it was announcing.
"""

from __future__ import annotations

from veloce import MCPContext, Veloce
from veloce.contrib.mcp._helpers import _notifier_var
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession

LIST_CHANGED = {
    "tools": "notifications/tools/list_changed",
    "prompts": "notifications/prompts/list_changed",
    "resources": "notifications/resources/list_changed",
}


def _app(*, prompts: bool = False, resources: bool = False) -> Veloce:
    app = Veloce(title="Negotiated", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Hide whatever it is asked to hide")
    async def lock(name: str, ctx: MCPContext) -> str:
        await ctx.hide(name)
        return "hidden"

    @app.mcp_tool(description="Show it again")
    async def unlock(name: str, ctx: MCPContext) -> str:
        await ctx.unhide(name)
        return "shown"

    @app.mcp_tool(description="Show everything again")
    async def reset(ctx: MCPContext) -> str:
        await ctx.reset_visibility()
        return "reset"

    @app.mcp_tool(description="A secret tool")
    async def secret_tool() -> int:
        return 42

    if prompts:

        @app.mcp_prompt(description="A secret prompt")
        async def secret_prompt() -> str:
            return "shh"

    if resources:

        @app.get(
            "/doc",
            expose_as_mcp_resource=True,
            mcp_resource_uri="doc://secret",
            mcp_description="A secret document",
        )
        async def doc() -> dict:
            return {}

    return app


def _session(*, persistent: bool = True) -> MCPSession:
    session = MCPSession()
    session.persistent = persistent
    return session


async def _advertise(app: Veloce, session: MCPSession) -> dict:
    response = await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "1"},
            },
        },
        session,
    )
    capabilities: dict = response["result"]["capabilities"]
    return capabilities


async def _frames(app: Veloce, session: MCPSession, tool: str, name: str = "") -> list[str]:
    """Drive one call and return the notification methods it sent."""
    sent: list[str] = []

    async def sink(message: dict) -> None:
        sent.append(message["method"])

    server = MCPServer(app)
    token = _notifier_var.set(sink)
    try:
        await server.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": {"name": name} if name else {}},
            },
            session,
        )
    finally:
        _notifier_var.reset(token)
    return sent


# ── Only the list that changed is announced ──────────────────────────


async def test_hiding_a_tool_announces_only_the_tool_list():
    """The other two frames used capabilities this server never advertised."""
    app = _app(prompts=True, resources=True)
    assert await _frames(app, _session(), "lock", "secret_tool") == [LIST_CHANGED["tools"]]


async def test_hiding_a_prompt_announces_only_the_prompt_list():
    app = _app(prompts=True, resources=True)
    assert await _frames(app, _session(), "lock", "secret_prompt") == [LIST_CHANGED["prompts"]]


async def test_hiding_a_resource_announces_only_the_resource_list():
    app = _app(prompts=True, resources=True)
    assert await _frames(app, _session(), "lock", "doc://secret") == [LIST_CHANGED["resources"]]


async def test_a_server_exposing_only_tools_sends_only_the_tool_frame():
    """The original violation: two frames for capabilities that were absent."""
    assert await _frames(_app(), _session(), "lock", "secret_tool") == [LIST_CHANGED["tools"]]


async def test_unhiding_announces_the_same_single_list():
    app = _app(prompts=True, resources=True)
    session = _session()
    await _frames(app, session, "lock", "secret_prompt")
    assert await _frames(app, session, "unlock", "secret_prompt") == [LIST_CHANGED["prompts"]]


async def test_hiding_several_kinds_announces_each_of_them_once():
    app = _app(prompts=True, resources=True)
    session = _session()

    @app.mcp_tool(description="Hide one of each")
    async def lock_all(ctx: MCPContext) -> str:
        await ctx.hide("secret_tool", "secret_prompt", "doc://secret")
        return "hidden"

    sent = await _frames(app, session, "lock_all")
    assert sorted(sent) == sorted(LIST_CHANGED.values())


async def test_resetting_announces_every_list_that_was_hidden():
    app = _app(prompts=True, resources=True)
    session = _session()
    await _frames(app, session, "lock", "secret_tool")
    await _frames(app, session, "lock", "secret_prompt")
    assert sorted(await _frames(app, session, "reset")) == sorted(
        [LIST_CHANGED["tools"], LIST_CHANGED["prompts"]]
    )


async def test_a_name_that_names_nothing_announces_nothing():
    """Nothing was listed under it, so no listing changed."""
    app = _app(prompts=True, resources=True)
    assert await _frames(app, _session(), "lock", "no-such-thing") == []


async def test_a_change_that_changes_nothing_still_says_nothing():
    app = _app()
    session = _session()
    await _frames(app, session, "lock", "secret_tool")
    assert await _frames(app, session, "lock", "secret_tool") == []


# ── The advertisement matches what can happen ────────────────────────


async def test_a_stateful_connection_is_told_its_lists_can_change():
    caps = await _advertise(_app(prompts=True, resources=True), _session())
    assert caps["tools"]["listChanged"] is True
    assert caps["prompts"]["listChanged"] is True
    assert caps["resources"]["listChanged"] is True


async def test_a_stateless_connection_is_told_they_cannot():
    """`hide` needs a connection, so on a stateless request no list can change."""
    caps = await _advertise(_app(prompts=True, resources=True), _session(persistent=False))
    assert caps["tools"]["listChanged"] is False
    assert caps["prompts"]["listChanged"] is False
    assert caps["resources"]["listChanged"] is False


async def test_subscribe_still_needs_the_subscription_machinery():
    """`listChanged` needs only the channel; `subscribe` needs more than that."""
    caps = await _advertise(_app(resources=True), _session())
    assert caps["resources"] == {"subscribe": False, "listChanged": True}


async def test_a_server_with_no_prompts_advertises_no_prompt_capability():
    caps = await _advertise(_app(), _session())
    assert "prompts" not in caps
    assert "resources" not in caps


async def test_every_frame_sent_names_an_advertised_capability():
    """The rule the violation broke, asserted directly."""
    app = _app(prompts=True, resources=True)
    session = _session()
    caps = await _advertise(app, session)

    @app.mcp_tool(description="Hide one of each")
    async def lock_all(ctx: MCPContext) -> str:
        await ctx.hide("secret_tool", "secret_prompt", "doc://secret")
        return "hidden"

    for frame in await _frames(app, session, "lock_all"):
        area = frame.removeprefix("notifications/").removesuffix("/list_changed")
        assert area in caps, f"{frame} names a capability that was not negotiated"
        assert caps[area]["listChanged"] is True
