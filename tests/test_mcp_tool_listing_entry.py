"""The memoized `tools/list` entry.

An entry is a pure function of registration data, so it is built once per tool and
reused. That is only safe while three things hold, and each is pinned here: the
memoized listing equals the freshly-built one, the entry carries nothing
caller- or revision-specific, and the per-request cache hints land on the enclosing
result rather than on an entry.
"""

from __future__ import annotations

from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer, _build_tool_listing_entry
from veloce.contrib.mcp.session import MCPSession

MODERN = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}


def _app(*, task_support: bool = False) -> Veloce:
    app = Veloce(title="ListingProbe", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="A plain tool", task_support=task_support)
    async def plain(value: int = 0) -> dict:
        return {"value": value}

    @app.get("/thing", expose_as_mcp_tool=True, mcp_description="A route-backed tool")
    async def route_tool(name: str = "x") -> dict:
        return {"name": name}

    @app.delete("/thing", expose_as_mcp_tool=True, mcp_description="A destructive tool")
    async def destructive() -> dict:
        return {"gone": True}

    return app


async def _list(server: MCPServer, meta: dict | None = MODERN, session=None) -> list[dict]:
    params: dict = {}
    if meta is not None:
        params["_meta"] = meta
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params},
        session if session is not None else MCPSession(),
    )
    return response["result"]["tools"]


# ── The memo equals the freshly built entry ──────────────────────────


async def test_a_listed_entry_equals_a_freshly_built_one():
    server = MCPServer(_app())
    listed = await _list(server)
    fresh = [_build_tool_listing_entry(tool) for tool in server.registry.tools.values()]
    assert listed == fresh


async def test_a_task_capable_tool_still_advertises_its_execution_block():
    server = MCPServer(_app(task_support=True))
    entry = next(e for e in await _list(server) if e["name"] == "plain")
    assert entry["execution"] == {"taskSupport": "optional"}


async def test_a_route_backed_tool_still_carries_its_annotations():
    server = MCPServer(_app())
    entry = next(e for e in await _list(server) if e["name"] == "destructive")
    assert entry["annotations"]["destructiveHint"] is True


async def test_every_required_field_survives_memoization():
    server = MCPServer(_app())
    for entry in await _list(server):
        assert entry["name"] and entry["description"]
        assert entry["inputSchema"]["type"] == "object"


# ── Repeated listings are stable ─────────────────────────────────────


async def test_two_listings_return_equal_payloads():
    server = MCPServer(_app())
    assert await _list(server) == await _list(server)


async def test_the_entry_is_reused_rather_than_rebuilt():
    """The memo is the point: a second listing must hand back the same object."""
    server = MCPServer(_app())
    first, second = await _list(server), await _list(server)
    assert all(a is b for a, b in zip(first, second, strict=True))


async def test_a_listing_is_stable_across_separate_sessions():
    server = MCPServer(_app())
    assert await _list(server, session=MCPSession()) == await _list(server, session=MCPSession())


# ── Nothing revision-specific is baked into an entry ──────────────────


async def test_both_revisions_receive_the_same_entries():
    """A handshake client and a modern one describe a tool identically."""
    server = MCPServer(_app())
    modern = await _list(server)
    handshake = await _list(server, meta=None)
    assert modern == handshake


# ── Cache hints never reach an entry ─────────────────────────────────


async def test_cache_hints_land_on_the_result_not_on_an_entry():
    server = MCPServer(_app())
    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MODERN}},
        MCPSession(),
    )
    result = response["result"]
    assert "ttlMs" in result and "cacheScope" in result
    for entry in result["tools"]:
        assert "ttlMs" not in entry and "cacheScope" not in entry


async def test_stamping_one_response_does_not_leak_into_the_next():
    """The regression this guards: hints written onto a shared entry would persist."""
    server = MCPServer(_app())
    await _list(server)
    for entry in await _list(server):
        assert "ttlMs" not in entry and "cacheScope" not in entry


# ── The memo is per tool, so filtering still works ────────────────────


async def test_a_visibility_filter_still_narrows_a_memoized_listing():
    def hide_destructive(tool, principal):
        return tool.name != "destructive"

    server = MCPServer(_app(), tool_filter=hide_destructive)
    names = [e["name"] for e in await _list(server)]
    assert "destructive" not in names
    assert "plain" in names


async def test_filtering_does_not_disturb_an_unfiltered_server():
    """Two servers over the same app must not observe each other's visibility."""
    app = _app()
    filtered = MCPServer(app, tool_filter=lambda tool, principal: tool.name == "plain")
    plain = MCPServer(app)
    assert [e["name"] for e in await _list(filtered)] == ["plain"]
    assert len(await _list(plain)) == 3
