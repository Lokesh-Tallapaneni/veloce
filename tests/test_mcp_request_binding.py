"""The request binding an MCP call makes is scoped to that call.

Invoking a tool binds a synthetic request so a handler, its dependencies, and
the hooks around it read `request` / `g` / `current_app` exactly as they do over
HTTP. That binding must be undone when the call ends: the Streamable HTTP
transport awaits `handle_message` from inside a live HTTP request, so a binding
that outlived the call would leave the remainder of that handler - and every
hook after it - reading the synthetic MCP request instead of the real one.
"""

from __future__ import annotations

import asyncio
import gc

import orjson

import veloce
from veloce import MCPContext, Request, TestClient, Veloce, g
from veloce._internal import _current_app_var, _current_request_var
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.helpers import has_request_context


def _app(**config) -> Veloce:
    app = Veloce(title="BindProbe", version="1.0.0", openapi_url=None)
    app.config.update(config)

    @app.mcp_tool(description="A tool")
    async def noop() -> int:
        return 1

    @app.mcp_tool(description="A failing tool")
    async def boom() -> int:
        raise ValueError("handler exploded")

    @app.get(
        "/settings",
        expose_as_mcp_resource=True,
        mcp_resource_uri="config://settings",
        mcp_description="Settings",
    )
    async def settings() -> dict:
        return {"debug": False}

    return app


async def _call(app: Veloce, method: str, params: dict) -> dict:
    return await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, MCPSession()
    )


# ── Nothing is left bound afterwards ─────────────────────────────────


async def test_a_tool_call_leaves_no_request_bound():
    await _call(_app(), "tools/call", {"name": "noop", "arguments": {}})
    assert has_request_context() is False


async def test_a_failing_tool_call_still_unbinds():
    """The unbind is in a `finally`; an error path must not skip it."""
    result = (await _call(_app(), "tools/call", {"name": "boom", "arguments": {}}))["result"]
    assert result["isError"] is True
    assert has_request_context() is False


async def test_a_timed_out_call_still_unbinds():
    app = _app(MCP_CALL_TIMEOUT=0.01)

    @app.mcp_tool(description="Sleeps past the budget")
    async def slow() -> int:
        await asyncio.sleep(5)
        return 1

    await _call(app, "tools/call", {"name": "slow", "arguments": {}})
    assert has_request_context() is False


async def test_a_resource_read_leaves_no_request_bound():
    await _call(_app(), "resources/read", {"uri": "config://settings"})
    assert has_request_context() is False


async def test_the_app_binding_is_released_too():
    await _call(_app(), "tools/call", {"name": "noop", "arguments": {}})
    assert _current_app_var.get() is None


async def test_a_bare_context_still_reports_no_state_after_a_call():
    """The guard on `MCPContext.state` must not be defeated by a stale binding."""
    import pytest

    await _call(_app(), "tools/call", {"name": "noop", "arguments": {}})
    with pytest.raises(RuntimeError, match="request being handled"):
        MCPContext("bare").state


# ── A caller's own request survives the call ─────────────────────────


def test_a_handler_keeps_its_own_request_across_a_tool_call():
    app = _app()

    @app.get("/wrap")
    async def wrap(request: Request) -> dict:
        before = _current_request_var.get()
        await MCPServer(app).handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "noop", "arguments": {}},
            },
            MCPSession(),
        )
        return {
            "same": _current_request_var.get() is before,
            "path": veloce.request.path,
            "is_mcp": veloce.request.is_mcp,
        }

    payload = TestClient(app).get("/wrap").json()
    assert payload == {"same": True, "path": "/wrap", "is_mcp": False}


def test_a_handler_keeps_its_own_globals_across_a_tool_call():
    """`g` is reset per call; a caller's stash must come back afterwards."""
    app = _app()

    @app.get("/stash")
    async def stash() -> dict:
        g.trace_id = "outer"
        await MCPServer(app).handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "noop", "arguments": {}},
            },
            MCPSession(),
        )
        return {"trace_id": g.get("trace_id", "<lost>")}

    assert TestClient(app).get("/stash").json() == {"trace_id": "outer"}


def test_an_after_request_hook_sees_the_real_request():
    """The transport dispatches inside a request; response hooks run after it."""
    app = _app()
    seen: list[dict] = []
    app.mount_mcp(transport="http", path="/mcp")

    @app.after_request
    async def record(request: Request, response):
        seen.append({"path": veloce.request.path, "is_mcp": veloce.request.is_mcp})
        return response

    client = TestClient(app)
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "noop", "arguments": {}},
        },
        headers={"accept": "application/json", "content-type": "application/json"},
    )
    assert response.status_code == 200
    assert seen[-1] == {"path": "/mcp", "is_mcp": False}


def test_the_tool_still_sees_its_own_binding_during_the_call():
    """Unbinding afterwards must not weaken the binding during the call."""
    app = Veloce(title="During", openapi_url=None)

    @app.mcp_tool(description="Reads the bound request")
    async def introspect() -> dict:
        return {"path": veloce.request.path, "is_mcp": veloce.request.is_mcp}

    @app.get("/outer")
    async def outer() -> dict:
        response = await MCPServer(app).handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "introspect", "arguments": {}},
            },
            MCPSession(),
        )
        return orjson.loads(response["result"]["content"][0]["text"])

    assert TestClient(app).get("/outer").json() == {"path": "/mcp/introspect", "is_mcp": True}


# ── A call abandoned mid-flight ──────────────────────────────────────


def test_a_call_abandoned_mid_flight_plants_no_binding_when_collected():
    """A finalized coroutine runs its `finally` wherever the collector is.

    Closing a suspended call throws `GeneratorExit` at the suspension point, and
    the collector may do that from any context - here, the caller's own. There is
    no awaiter to hand a context back to, so the unbind must not fire and write
    the outer binding into a context that never made the call.
    """
    app = Veloce(title="Abandoned", openapi_url=None)

    @app.mcp_tool(description="Never settles")
    async def stalls() -> int:
        await asyncio.sleep(60)
        return 1

    server = MCPServer(app)
    loop = asyncio.new_event_loop()
    # The task is abandoned on purpose; asyncio's default handler would report it.
    loop.set_exception_handler(lambda loop, context: None)
    try:
        task = loop.create_task(
            server.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "stalls", "arguments": {}},
                },
                MCPSession(),
            )
        )
        # Let the call reach its `await`, so finalization has a frame to close.
        for _ in range(8):
            loop.run_until_complete(asyncio.sleep(0))
        assert not task.done()
        coro = task.get_coro()
    finally:
        loop.close()

    # Finalize it from here - the caller's context, not the one that made the
    # call - which is what the collector does to an abandoned coroutine.
    del task
    gc.collect()
    coro.close()

    assert has_request_context() is False
    assert _current_app_var.get() is None
    # The cancellation registry is released too: a failed unbind must not abandon
    # the rest of the cleanup and strand the entry.
    assert server._inflight == {}
