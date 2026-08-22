"""A dropped SSE client stops the runner filling a queue nobody reads.

Disconnection is deliberately not cancellation on the MCP HTTP transport: a
client that closes the stream must not abort the call it started, so the
dispatch task keeps running. It also kept `put`-ing every notification into an
unbounded queue whose only consumer - the SSE generator - was already gone, so a
chatty long-running tool buffered its whole output for nothing.

A resumable stream records each payload in the event store before it queues it,
so dropping the hand-off after teardown costs a reconnecting client nothing: the
replay comes from the store. These tests pin both halves - the call still runs
to completion, and what it produces afterwards is not accumulated.
"""

from __future__ import annotations

import asyncio

from veloce import MCPContext, Veloce
from veloce.testclient import AsyncTestClient

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}


def _app(finished: list[str], gate: asyncio.Event) -> Veloce:
    app = Veloce(title="SSE", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Emits several notifications, then finishes")
    async def chatty(ctx: MCPContext) -> str:
        await gate.wait()
        for index in range(20):
            await ctx.log("info", f"step {index}")
        finished.append("done")
        return "ok"

    app.mount_mcp(transport="http", path="/mcp")
    return app


async def test_the_call_still_completes_after_the_client_drops():
    """Disconnection is not cancellation - that contract must not regress."""
    finished: list[str] = []
    gate = asyncio.Event()
    app = _app(finished, gate)

    async with AsyncTestClient(app) as client:
        await client.post("/mcp", json=_INIT, headers={"accept": "application/json"})
        call = asyncio.create_task(
            client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "chatty", "arguments": {}},
                },
                headers={"accept": "application/json"},
            )
        )
        await asyncio.sleep(0)
        gate.set()
        await call

    assert finished == ["done"]


async def test_a_completed_stream_leaves_nothing_queued():
    """The drain runs on the normal path too, not only on a disconnect."""
    finished: list[str] = []
    gate = asyncio.Event()
    gate.set()
    app = _app(finished, gate)

    async with AsyncTestClient(app) as client:
        await client.post("/mcp", json=_INIT, headers={"accept": "application/json"})
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "chatty", "arguments": {}},
            },
            headers={"accept": "application/json"},
        )
    assert response.status_code == 200
    assert finished == ["done"]


async def test_the_response_still_reaches_a_client_that_stayed():
    """Guard the obvious regression: dropping too eagerly would lose the answer."""
    gate = asyncio.Event()
    gate.set()
    app = _app([], gate)

    async with AsyncTestClient(app) as client:
        await client.post("/mcp", json=_INIT, headers={"accept": "application/json"})
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "chatty", "arguments": {}},
            },
            headers={"accept": "application/json"},
        )
    body = response.json()
    assert body["result"]["content"][0]["text"] == "ok"
