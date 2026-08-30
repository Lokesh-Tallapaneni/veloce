"""`_meta` a handler sends back on its own result.

The definition-level `_meta` describes a tool; this describes one *call* of it —
what it cost, a trace id, whatever an extension the client understands asks for.
The protocol reserves the field on every result type for exactly this, and a
handler had no way to reach it.

It belongs to one call. The slot is bound per message, so nothing a handler
attaches can reach the next client's response.
"""

from __future__ import annotations

from tests._mcp import call_tool
from veloce import MCPContext, Veloce
from veloce.contrib.mcp._helpers import _attach_result_meta
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession

# ── A handler can attach it ──────────────────────────────────────────


async def test_a_handler_attaches_meta_to_its_result():
    app = Veloce(title="Meta", openapi_url=None)

    @app.mcp_tool(description="Reports what it cost")
    async def priced(ctx: MCPContext) -> dict:
        ctx.result_meta["io.example/cost"] = 0.02
        return {"ok": True}

    assert (await call_tool(app, "priced"))["_meta"] == {"io.example/cost": 0.02}


async def test_several_keys_may_be_attached():
    app = Veloce(title="Meta2", openapi_url=None)

    @app.mcp_tool(description="Reports several things")
    async def annotated(ctx: MCPContext) -> dict:
        ctx.result_meta["cost"] = 0.02
        ctx.result_meta["trace"] = "abc123"
        return {"ok": True}

    assert (await call_tool(app, "annotated"))["_meta"] == {"cost": 0.02, "trace": "abc123"}


async def test_a_handler_that_attaches_nothing_sends_no_meta():
    app = Veloce(title="Meta3", openapi_url=None)

    @app.mcp_tool(description="Says nothing extra")
    async def plain() -> int:
        return 1

    assert "_meta" not in await call_tool(app, "plain")


async def test_reading_the_slot_without_writing_sends_nothing():
    """Touching the property must not create an empty `_meta` on the wire."""
    app = Veloce(title="Meta4", openapi_url=None)

    @app.mcp_tool(description="Looks but does not write")
    async def looks(ctx: MCPContext) -> dict:
        assert ctx.result_meta == {}
        return {"ok": True}

    assert "_meta" not in await call_tool(app, "looks")


async def test_the_result_content_is_unaffected():
    app = Veloce(title="Meta5", openapi_url=None)

    @app.mcp_tool(description="Still returns its answer")
    async def answer(ctx: MCPContext) -> dict:
        ctx.result_meta["cost"] = 1
        return {"value": 42}

    result = await call_tool(app, "answer")
    assert result["content"][0]["text"] == '{"value":42}'


# ── It belongs to one call ───────────────────────────────────────────


async def test_meta_does_not_leak_into_the_next_call():
    app = Veloce(title="Scope", openapi_url=None)

    @app.mcp_tool(description="Attaches")
    async def attaches(ctx: MCPContext) -> dict:
        ctx.result_meta["seen"] = True
        return {"ok": True}

    @app.mcp_tool(description="Attaches nothing")
    async def quiet() -> int:
        return 1

    assert (await call_tool(app, "attaches"))["_meta"] == {"seen": True}
    assert "_meta" not in await call_tool(app, "quiet")


async def test_each_call_starts_with_an_empty_slot():
    app = Veloce(title="Scope2", openapi_url=None)

    @app.mcp_tool(description="Counts what it sees")
    async def counter(ctx: MCPContext) -> dict:
        ctx.result_meta["calls"] = ctx.result_meta.get("calls", 0) + 1
        return {"ok": True}

    assert (await call_tool(app, "counter"))["_meta"] == {"calls": 1}
    assert (await call_tool(app, "counter"))["_meta"] == {"calls": 1}


# ── Protocol machinery keeps precedence ──────────────────────────────


async def test_a_handler_cannot_displace_the_protocol_own_meta():
    """A task marker or subscription id answers the client's request, not the tool's."""

    result = {"content": [], "_meta": {"protocol": "owns-this"}}
    attached = _attach_result_meta(result, {"protocol": "handler-tried", "mine": 1})
    assert attached["_meta"]["protocol"] == "owns-this"
    assert attached["_meta"]["mine"] == 1


# ── A resource read carries it too ───────────────────────────────────


async def test_a_resource_read_can_attach_meta():
    app = Veloce(title="Res", openapi_url=None)

    @app.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="doc://one",
        mcp_description="A document",
    )
    async def doc(ctx: MCPContext) -> dict:
        ctx.result_meta["io.example/generated"] = True
        return {"text": "hello"}

    response = await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "doc://one"}},
        MCPSession(),
    )
    assert response["result"]["_meta"] == {"io.example/generated": True}


# ── Off a dispatch ───────────────────────────────────────────────────


def test_a_bare_context_still_offers_the_slot():
    """No request is needed to stage metadata; it is only sent if a call is running."""
    context = MCPContext("bare")
    context.result_meta["staged"] = True
    assert context.result_meta == {"staged": True}
