"""Caching hints on the results the spec requires them for.

The load-bearing half is the scope. A list that can differ between two authorized
callers must be marked private, or a shared gateway may serve one caller's answer to
another — outside any check this server performs.
"""

from __future__ import annotations

import pytest

from tests._mcp import initialize
from veloce import Veloce
from veloce.contrib.mcp.server import DEFAULT_CACHE_TTL_MS, MCPServer
from veloce.contrib.mcp.session import MCPSession

MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}
CACHEABLE = [
    "tools/list",
    "prompts/list",
    "resources/list",
    "resources/templates/list",
    "server/discover",
]


def _app(
    *, scoped_tool: bool = False, scoped_prompt: bool = False, scoped_resource: bool = False
) -> Veloce:
    app = Veloce(title="CacheProbe", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Open tool")
    async def open_tool() -> dict:
        return {"ok": True}

    if scoped_tool:

        @app.mcp_tool(description="Scoped tool", scopes=["admin"])
        async def scoped() -> dict:
            return {"ok": True}

    @app.mcp_prompt(description="Open prompt")
    async def open_prompt() -> str:
        return "hi"

    if scoped_prompt:

        @app.mcp_prompt(description="Scoped prompt", scopes=["admin"])
        async def scoped_prompt_fn() -> str:
            return "hi"

    @app.get(
        "/config",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://app",
        mcp_description="Config",
    )
    async def config() -> dict:
        return {"theme": "dark"}

    if scoped_resource:

        @app.get(
            "/secrets",
            expose_as_mcp_resource=True,
            mcp_resource_uri="config://secrets",
            mcp_description="Secrets",
            mcp_scopes=["admin"],
        )
        async def secrets() -> dict:
            return {}

    return app


async def _call(server: MCPServer, method: str, params: dict | None = None) -> dict:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {**(params or {}), "_meta": MODERN},
    }
    return await server.handle_message(message)


# ── Presence ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", CACHEABLE)
async def test_cacheable_results_carry_hints(method: str):
    response = await _call(MCPServer(_app()), method)
    result = response["result"]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] == DEFAULT_CACHE_TTL_MS
    assert result["cacheScope"] in {"public", "private"}


async def test_resources_read_carries_hints():
    response = await _call(MCPServer(_app()), "resources/read", {"uri": "config://app"})
    result = response["result"]
    assert result["ttlMs"] == DEFAULT_CACHE_TTL_MS
    assert result["cacheScope"] == "private"


async def test_a_non_cacheable_method_carries_no_hints():
    """`prompts/get` renders one prompt and is not on the cacheable list."""
    response = await _call(
        MCPServer(_app()), "prompts/get", {"name": "open_prompt", "arguments": {}}
    )
    assert "ttlMs" not in response["result"]
    assert "cacheScope" not in response["result"]


async def test_tools_call_carries_no_hints():
    server = MCPServer(_app())
    response = await _call(server, "tools/call", {"name": "open_tool", "arguments": {}})
    assert "ttlMs" not in response["result"]
    assert "cacheScope" not in response["result"]


# ── Scope ────────────────────────────────────────────────────────────


async def test_an_unfiltered_uniform_tool_list_is_public():
    response = await _call(MCPServer(_app()), "tools/list")
    assert response["result"]["cacheScope"] == "public"


async def test_a_tool_list_narrowed_by_a_filter_is_private():
    response = await _call(MCPServer(_app(), tool_filter=lambda t, p: True), "tools/list")
    assert response["result"]["cacheScope"] == "private"


async def test_a_tool_list_containing_a_scoped_tool_is_private():
    """Declared scopes make the list caller-dependent even with no filter set."""
    response = await _call(MCPServer(_app(scoped_tool=True)), "tools/list")
    assert response["result"]["cacheScope"] == "private"


async def test_a_prompt_list_containing_a_scoped_prompt_is_private():
    response = await _call(MCPServer(_app(scoped_prompt=True)), "prompts/list")
    assert response["result"]["cacheScope"] == "private"


async def test_an_unscoped_prompt_list_is_public():
    response = await _call(MCPServer(_app()), "prompts/list")
    assert response["result"]["cacheScope"] == "public"


async def test_a_resource_list_containing_a_scoped_resource_is_private():
    """It omits what this caller may not read, so it is this caller's answer."""
    response = await _call(MCPServer(_app(scoped_resource=True)), "resources/list")
    assert response["result"]["cacheScope"] == "private"


async def test_an_unscoped_resource_list_is_public():
    response = await _call(MCPServer(_app()), "resources/list")
    assert response["result"]["cacheScope"] == "public"


# ── Configuration ────────────────────────────────────────────────────


async def test_the_ttl_is_configurable():
    response = await _call(MCPServer(_app(), cache_ttl_ms=1234), "tools/list")
    assert response["result"]["ttlMs"] == 1234


async def test_a_zero_ttl_marks_results_immediately_stale():
    response = await _call(MCPServer(_app(), cache_ttl_ms=0), "tools/list")
    assert response["result"]["ttlMs"] == 0


async def test_a_negative_ttl_is_clamped_to_zero():
    """The spec requires a value >= 0; a mistake must not emit a negative hint."""
    response = await _call(MCPServer(_app(), cache_ttl_ms=-5), "tools/list")
    assert response["result"]["ttlMs"] == 0


def test_mount_mcp_accepts_a_cache_ttl():
    app = _app()
    coro = app.mount_mcp(cache_ttl_ms=60_000)
    coro.close()


# ── Era gating ───────────────────────────────────────────────────────


async def test_a_handshake_era_client_sees_no_hints():
    """The handshake revisions have no such fields; they must not leak into them."""
    server = MCPServer(_app())
    await server.handle_message(
        initialize("2025-11-25", id=1, client_info={"name": "c", "version": "1"})
    )
    await server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    response = await server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert "ttlMs" not in response["result"]
    assert "cacheScope" not in response["result"]
    assert "resultType" not in response["result"]


# ── A listing a connection can narrow is that connection's answer ────


def _stateful_session() -> MCPSession:
    session = MCPSession()
    session.persistent = True
    return session


async def _list_scope(app: Veloce, method: str, session: MCPSession | None) -> str:
    response = await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {"_meta": MODERN},
        },
        session or MCPSession(),
    )
    scope: str = response["result"]["cacheScope"]
    return scope


async def test_a_listing_on_a_stateful_connection_is_private():
    """`MCPContext.hide` can narrow it at any point, for this connection only."""
    assert await _list_scope(_app(), "tools/list", _stateful_session()) == "private"


async def test_every_listing_on_a_stateful_connection_is_private():
    app = _app(scoped_prompt=False)
    for method in ("tools/list", "prompts/list", "resources/list"):
        assert await _list_scope(app, method, _stateful_session()) == "private", method


async def test_a_listing_on_a_stateless_request_stays_public():
    """Nothing it is told survives the response, so nothing can narrow it."""
    stateless = MCPSession()
    stateless.persistent = False
    assert await _list_scope(_app(), "tools/list", stateless) == "public"


async def test_a_stateful_connection_is_private_even_before_anything_is_hidden():
    """Which connection hid something is not a question a cache key can ask."""
    session = _stateful_session()
    assert not session.hidden
    assert await _list_scope(_app(), "tools/list", session) == "private"


# ── `server/discover` is never publicly cacheable ────────────────────


async def test_discover_is_private_on_a_stateless_connection():
    """Its answer is built for the asking connection, so it must not be shared.

    `public` invites a shared gateway to serve one caller's answer to another.
    The capability block reflects what that connection can be told and which
    protocol revision it stated, and the result carries `instructions` - server
    prose a client feeds to its model.
    """
    server = MCPServer(_app())
    result = (await _call(server, "server/discover"))["result"]
    assert result["cacheScope"] == "private"


async def test_discover_is_private_on_a_stateful_connection():
    server = MCPServer(_app())
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": MODERN}},
        MCPSession(persistent=True),
    )
    assert response["result"]["cacheScope"] == "private"


async def test_the_discover_answer_really_does_vary_by_revision():
    """The reason it cannot be public, stated as a test rather than a comment."""
    server = MCPServer(_app())
    modern = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": MODERN}},
        MCPSession(),
    )
    handshake = await server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "server/discover", "params": {}},
        MCPSession(),
    )
    assert set(modern["result"]["capabilities"]) != set(handshake["result"]["capabilities"])


async def test_a_stateless_tool_listing_is_still_publicly_cacheable():
    """The rest of the classification is unchanged - this is one method, not a policy."""
    server = MCPServer(_app())
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MODERN}},
        MCPSession(persistent=False),
    )
    assert response["result"]["cacheScope"] == "public"


async def test_a_scoped_tool_listing_is_still_private():
    server = MCPServer(_app(scoped_tool=True))
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MODERN}},
        MCPSession(persistent=False),
    )
    assert response["result"]["cacheScope"] == "private"
