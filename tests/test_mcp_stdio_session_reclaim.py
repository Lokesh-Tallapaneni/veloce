"""Closing a stdio connection reclaims what it owned.

`StdioTransport.serve` unregistered the connection on EOF, which dropped its
notification sink and listen streams - but left the session's tasks in the
registry. `TaskRegistry.evict_expired` deliberately never reaps a task that has
not settled (so a client polling a slow task is never robbed of its result), so
a never-settling task created over stdio outlived its connection, along with the
asyncio runner still executing it, for the lifetime of the process.

The HTTP transport already reclaimed this through `evict_session` on idle TTL.
This asserts stdio does the same, which is the asymmetry that was the bug.
"""

from __future__ import annotations

import asyncio

import orjson

from veloce import Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.transports.stdio import StdioTransport


def _app() -> Veloce:
    app = Veloce(title="Stdio", version="1.0.0", openapi_url=None)
    # Without this the server builds no `ConnectionRegistry` at all, and an
    # assertion about what EOF unregisters has nothing to look at.
    app.config["MCP_RESOURCE_SUBSCRIPTIONS"] = True

    @app.mcp_tool(description="Never settles on its own", task_support=True)
    async def forever() -> str:
        await asyncio.Event().wait()
        return "unreachable"

    @app.mcp_tool(description="Returns at once")
    async def quick() -> str:
        return "ok"

    return app


def _line(payload: dict) -> bytes:
    return orjson.dumps(payload)


async def _run(lines: list[dict]) -> tuple[MCPServer, list[bytes]]:
    """Drive a stdio transport over the given client lines, then EOF."""
    server = MCPServer(_app())
    pending = [_line(item) for item in lines]
    written: list[bytes] = []

    async def read_line() -> bytes | None:
        return pending.pop(0) if pending else None

    async def write_line(data: bytes) -> None:
        written.append(data)

    transport = StdioTransport(server, read_line, write_line)
    await transport.serve()
    return server, written


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


_URI = "res://ledger"

_SUBSCRIBE = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "resources/subscribe",
    "params": {"uri": _URI},
}


async def test_a_never_settling_task_is_reclaimed_on_eof():
    """The defect: this task and its runner survived the connection."""
    server, _ = await _run(
        [
            _INIT,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "forever", "arguments": {}, "task": {"ttl": 600000}},
            },
        ]
    )
    assert server._tasks.tasks == {}, "a task outlived the connection that created it"


async def test_the_runner_is_cancelled_not_merely_dropped():
    """Dropping the record while the coroutine ran would still leak the work."""
    server, _ = await _run(
        [
            _INIT,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "forever", "arguments": {}, "task": {"ttl": 600000}},
            },
        ]
    )
    await asyncio.sleep(0)
    assert not [t for t in asyncio.all_tasks() if "forever" in repr(t) and not t.done()]
    assert server._tasks.tasks == {}


async def test_a_connection_with_no_tasks_closes_cleanly():
    """The reclaim must not disturb the ordinary path.

    `quick` is not task-augmented, so the emptiness of the task registry says
    nothing here - it is empty whatever the reclaim does. What the ordinary path
    owes the client is its result, so that is what this asserts.
    """
    server, written = await _run(
        [
            _INIT,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "quick", "arguments": {}},
            },
        ]
    )
    replies = {reply["id"]: reply for reply in (orjson.loads(line) for line in written)}
    assert 2 in replies, f"the call was never answered: {list(replies)}"
    assert "error" not in replies[2], replies[2]
    assert "ok" in orjson.dumps(replies[2]["result"]).decode()
    assert server._tasks.tasks == {}


async def test_the_connection_sink_is_still_unregistered():
    """Reclaiming tasks must not leave the connection registered."""
    server, _ = await _run([_INIT])
    registry = server._connections
    assert registry is not None, "no registry exists for this to assert against"
    assert not registry._sinks


async def test_a_closed_connection_receives_no_further_notification():
    """What dropping the connection exists to guarantee, stated observably.

    An emptiness check on `_sinks` cannot carry this claim: `evict_session`
    drops every token the session held, so it passes whether or not the
    connection was dropped at all. Fan-out reaching nobody is the property, and
    the recorded open state is what stops this passing on a session that never
    subscribed.
    """
    server = MCPServer(_app())
    written: list[bytes] = []
    open_state: list[tuple[int, bool]] = []
    pending = [_line(_INIT), _line(_SUBSCRIBE)]

    async def read_line() -> bytes | None:
        return pending.pop(0) if pending else None

    async def write_line(data: bytes) -> None:
        # Sampled here rather than at EOF: a reply is written only once its
        # request has been dispatched, so the subscribe is visible by the last
        # one, while the read loop runs ahead of the tasks it creates.
        written.append(data)
        registry = server._connections
        session = transport._session
        assert registry is not None and session is not None
        open_state.append((len(registry._sinks), _URI in session.subscriptions))

    transport = StdioTransport(server, read_line, write_line)
    await transport.serve()

    assert open_state[-1] == (1, True), f"the connection never subscribed: {open_state}"
    at_eof = len(written)
    await server.notify_resource_updated(_URI)
    assert len(written) == at_eof, "a closed connection was still sent a notification"
