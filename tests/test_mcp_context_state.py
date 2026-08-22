"""Per-call scratch space on the tool context.

A handler already had per-call state through `request.state`, but only by
declaring a second parameter it otherwise had no use for. `MCPContext.state`
exposes that same store, so a handler holding the context can stash a value
without asking for the request as well.

It is deliberately the *same* object rather than a second store: a dependency
writing through `request.state` and a handler reading through `ctx.state` must
never see different data.
"""

from __future__ import annotations

import pytest

from veloce import Depends, MCPContext, Request, Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


async def _call(app: Veloce, name: str, arguments: dict | None = None) -> dict:
    response = await MCPServer(app).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        MCPSession(),
    )
    result = response["result"]
    assert not result.get("isError"), result["content"][0]["text"]
    import orjson

    return orjson.loads(result["content"][0]["text"])


# ── Reading and writing ──────────────────────────────────────────────


async def test_a_handler_can_stash_and_read_a_value():
    app = Veloce(title="StateProbe", openapi_url=None)

    @app.mcp_tool(description="Stash a value")
    async def stash(ctx: MCPContext) -> dict:
        ctx.state.marker = "written"
        return {"marker": ctx.state.marker}

    assert (await _call(app, "stash"))["marker"] == "written"


async def test_attribute_and_item_access_reach_the_same_value():
    app = Veloce(title="StateProbe2", openapi_url=None)

    @app.mcp_tool(description="Both syntaxes")
    async def both(ctx: MCPContext) -> dict:
        ctx.state.by_attr = 1
        ctx.state["by_item"] = 2
        return {"attr": ctx.state["by_attr"], "item": ctx.state.by_item}

    assert await _call(app, "both") == {"attr": 1, "item": 2}


async def test_get_returns_a_default_for_an_unset_key():
    app = Veloce(title="StateProbe3", openapi_url=None)

    @app.mcp_tool(description="Unset key")
    async def unset(ctx: MCPContext) -> dict:
        return {"value": ctx.state.get("never_set", "fallback")}

    assert (await _call(app, "unset"))["value"] == "fallback"


# ── One store, not two ───────────────────────────────────────────────


async def test_the_context_and_the_request_share_one_store():
    app = Veloce(title="SharedProbe", openapi_url=None)

    @app.mcp_tool(description="Same object")
    async def same(ctx: MCPContext, request: Request) -> dict:
        return {"identical": ctx.state is request.state}

    assert (await _call(app, "same"))["identical"] is True


async def test_a_dependency_write_is_visible_through_the_context():
    app = Veloce(title="DepProbe", openapi_url=None)

    def seed(request: Request) -> str:
        request.state.from_dep = "set-by-dependency"
        return "ok"

    @app.mcp_tool(description="Reads what a dependency wrote")
    async def reader(ctx: MCPContext, _seeded: str = Depends(seed)) -> dict:
        return {"seen": ctx.state.get("from_dep")}

    assert (await _call(app, "reader"))["seen"] == "set-by-dependency"


async def test_a_context_write_is_visible_to_the_request():
    app = Veloce(title="DepProbe2", openapi_url=None)

    @app.mcp_tool(description="Writes through the context")
    async def writer(ctx: MCPContext, request: Request) -> dict:
        ctx.state.written = "via-context"
        return {"seen_by_request": request.state.get("written")}

    assert (await _call(app, "writer"))["seen_by_request"] == "via-context"


# ── Scoped to one call ───────────────────────────────────────────────


async def test_state_does_not_leak_into_the_next_call():
    """Per-call: a later `tools/call` starts clean, as the store is the request's."""
    app = Veloce(title="ScopeProbe", openapi_url=None)

    @app.mcp_tool(description="Counts within one call")
    async def counter(ctx: MCPContext) -> dict:
        ctx.state.hits = ctx.state.get("hits", 0) + 1
        return {"hits": ctx.state.hits}

    assert (await _call(app, "counter"))["hits"] == 1
    assert (await _call(app, "counter"))["hits"] == 1


async def test_two_tools_in_one_server_do_not_share_state():
    app = Veloce(title="ScopeProbe2", openapi_url=None)

    @app.mcp_tool(description="Writes")
    async def put(ctx: MCPContext) -> dict:
        ctx.state.token = "abc"
        return {"ok": True}

    @app.mcp_tool(description="Reads")
    async def take(ctx: MCPContext) -> dict:
        return {"token": ctx.state.get("token")}

    await _call(app, "put")
    assert (await _call(app, "take"))["token"] is None


# ── Off a real invocation ────────────────────────────────────────────


def test_a_bare_context_explains_why_there_is_no_state():
    """A context built outside a call has no request, so no per-call store."""
    with pytest.raises(RuntimeError, match="request being handled"):
        MCPContext("bare").state


# ── A route-backed tool behaves the same ─────────────────────────────


async def test_a_route_backed_tool_shares_the_same_store():
    app = Veloce(title="RouteState", openapi_url=None)

    @app.get("/probe", expose_as_mcp_tool=True, mcp_description="Route-backed")
    async def probe(ctx: MCPContext, request: Request) -> dict:
        ctx.state.tagged = "yes"
        return {"identical": ctx.state is request.state, "tagged": request.state.get("tagged")}

    payload = await _call(app, "probe")
    assert payload == {"identical": True, "tagged": "yes"}
