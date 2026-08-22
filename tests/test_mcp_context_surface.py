"""What an `MCPContext` exposes to a running tool.

Call metadata (who is connected, what they support, whether this is a task), the
logging shorthands, and reading the server's own resources and prompts from inside a
tool. The last of those must not become an authorization bypass, which is pinned
here rather than left to review.
"""

from __future__ import annotations

import pytest

from veloce import Principal, Veloce
from veloce.contrib.mcp.context import MCPContext, _in_task_var
from veloce.contrib.mcp.errors import AuthorizationError
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.principal import set_principal

# ── Call metadata ────────────────────────────────────────────────────


def test_metadata_is_empty_off_a_session():
    """A bare context answers without a session rather than raising."""
    ctx = MCPContext("probe")
    assert ctx.session_id is None
    assert ctx.client_info == {}
    assert ctx.client_capabilities == {}
    assert ctx.is_background_task is False


def test_session_id_and_client_info_come_from_the_connection():
    session = MCPSession()
    session.client_info = {"name": "probe-client", "version": "2.1"}
    ctx = MCPContext("probe", session=session)
    assert ctx.session_id == session.public_id
    assert ctx.client_info == {"name": "probe-client", "version": "2.1"}


def test_client_capabilities_derive_from_the_session():
    session = MCPSession()
    session.client_capabilities = {"sampling": {}, "roots": {"listChanged": True}}
    ctx = MCPContext("probe", session=session)
    assert ctx.client_capabilities == {"sampling": {}, "roots": {"listChanged": True}}


def test_explicit_capabilities_still_win_for_a_bare_construction():
    """The pre-existing keyword keeps working for callers that build a context."""
    ctx = MCPContext("probe", client_capabilities={"sampling": {}})
    assert ctx.client_capabilities == {"sampling": {}}


def test_client_supports_reads_nested_capabilities():
    session = MCPSession()
    session.client_capabilities = {"sampling": {"tools": {}}, "elicitation": {}}
    ctx = MCPContext("probe", session=session)
    assert ctx.client_supports("sampling") is True
    assert ctx.client_supports("sampling.tools") is True
    assert ctx.client_supports("sampling.missing") is False
    assert ctx.client_supports("roots") is False


def test_client_supports_treats_an_explicit_false_as_unsupported():
    session = MCPSession()
    session.client_capabilities = {"roots": {"listChanged": False}}
    ctx = MCPContext("probe", session=session)
    assert ctx.client_supports("roots") is True
    assert ctx.client_supports("roots.listChanged") is False


def test_is_background_task_follows_the_task_flag():
    ctx = MCPContext("probe")
    token = _in_task_var.set(True)
    try:
        assert ctx.is_background_task is True
    finally:
        _in_task_var.reset(token)


# ── Logging shorthands ───────────────────────────────────────────────


async def test_level_shorthands_send_the_matching_level():
    sent: list[dict] = []

    async def notifier(message: dict) -> None:
        sent.append(message)

    ctx = MCPContext("probe", notifier=notifier)
    await ctx.debug("d")
    await ctx.info("i")
    await ctx.warning("w")
    await ctx.error("e")
    assert [m["params"]["level"] for m in sent] == ["debug", "info", "warning", "error"]
    assert [m["params"]["data"] for m in sent] == ["d", "i", "w", "e"]


async def test_level_shorthands_respect_the_client_minimum_level():
    sent: list[dict] = []

    async def notifier(message: dict) -> None:
        sent.append(message)

    ctx = MCPContext("probe", notifier=notifier, log_level="warning")
    await ctx.debug("dropped")
    await ctx.info("dropped")
    await ctx.error("kept")
    assert [m["params"]["level"] for m in sent] == ["error"]


async def test_level_shorthands_are_inert_without_a_channel():
    ctx = MCPContext("probe")
    await ctx.info("nowhere to go")


# ── Notifications ────────────────────────────────────────────────────


async def test_send_notification_emits_a_jsonrpc_notification():
    sent: list[dict] = []

    async def notifier(message: dict) -> None:
        sent.append(message)

    ctx = MCPContext("probe", notifier=notifier)
    await ctx.send_notification("notifications/custom", {"a": 1})
    assert sent == [{"jsonrpc": "2.0", "method": "notifications/custom", "params": {"a": 1}}]


async def test_send_notification_omits_absent_params():
    sent: list[dict] = []

    async def notifier(message: dict) -> None:
        sent.append(message)

    await MCPContext("probe", notifier=notifier).send_notification("notifications/ping")
    assert sent == [{"jsonrpc": "2.0", "method": "notifications/ping"}]


async def test_send_notification_is_inert_without_a_channel():
    await MCPContext("probe").send_notification("notifications/custom")


# ── Reading the server's own components ──────────────────────────────


def _server_app() -> Veloce:
    app = Veloce(title="ContextProbe", openapi_url=None)

    @app.get(
        "/config",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
        mcp_description="App configuration",
    )
    async def config() -> dict:
        return {"theme": "dark"}

    @app.mcp_prompt(description="Greet someone")
    async def greet(name: str) -> str:
        return f"Hello {name}"

    @app.mcp_tool(description="Read config through the context")
    async def via_context(ctx: MCPContext) -> dict:
        return await ctx.read_resource("config://app")

    @app.mcp_tool(description="Render a prompt through the context")
    async def prompt_via_context(ctx: MCPContext) -> dict:
        return await ctx.get_prompt("greet", {"name": "ada"})

    @app.mcp_tool(description="Enumerate the server's components")
    async def inventory(ctx: MCPContext) -> dict:
        return {
            "resources": [r["uri"] for r in ctx.list_resources()],
            "prompts": [p["name"] for p in ctx.list_prompts()],
        }

    return app


async def test_tool_reads_a_resource_through_the_context():
    server = MCPServer(_server_app())
    result = await server._tools_call({"name": "via_context", "arguments": {}})
    assert "theme" in result["content"][0]["text"]


async def test_tool_renders_a_prompt_through_the_context():
    server = MCPServer(_server_app())
    result = await server._tools_call({"name": "prompt_via_context", "arguments": {}})
    assert "Hello ada" in result["content"][0]["text"]


async def test_tool_enumerates_resources_and_prompts():
    server = MCPServer(_server_app())
    result = await server._tools_call({"name": "inventory", "arguments": {}})
    text = result["content"][0]["text"]
    assert "config://app" in text
    assert "greet" in text


async def test_reading_a_scoped_resource_still_enforces_its_scopes():
    """A tool must not become a way around a resource's own authorization."""
    app = Veloce(title="ScopedProbe", openapi_url=None)

    @app.get(
        "/secret",
        expose_as_mcp_resource=True,
        mcp_resource_uri="secret://vault",
        mcp_description="Sensitive",
        mcp_scopes=["vault"],
    )
    async def secret() -> dict:
        return {"token": "s3cr3t"}

    @app.mcp_tool(description="Try to read the vault")
    async def leak(ctx: MCPContext) -> dict:
        return await ctx.read_resource("secret://vault")

    server = MCPServer(app)
    set_principal(Principal(subject="nobody", scopes=frozenset()))
    with pytest.raises(AuthorizationError):
        await server._tools_call({"name": "leak", "arguments": {}})


async def test_reading_a_scoped_resource_succeeds_with_the_scope():
    app = Veloce(title="ScopedProbe", openapi_url=None)

    @app.get(
        "/secret",
        expose_as_mcp_resource=True,
        mcp_resource_uri="secret://vault",
        mcp_description="Sensitive",
        mcp_scopes=["vault"],
    )
    async def secret() -> dict:
        return {"token": "s3cr3t"}

    @app.mcp_tool(description="Read the vault", scopes=["vault"])
    async def read_vault(ctx: MCPContext) -> dict:
        return await ctx.read_resource("secret://vault")

    server = MCPServer(app)
    set_principal(Principal(subject="ops", scopes=frozenset({"vault"})))
    result = await server._tools_call({"name": "read_vault", "arguments": {}})
    assert "s3cr3t" in result["content"][0]["text"]


def test_component_access_explains_itself_off_a_server():
    ctx = MCPContext("probe")
    with pytest.raises(RuntimeError, match="list_resources"):
        ctx.list_resources()
