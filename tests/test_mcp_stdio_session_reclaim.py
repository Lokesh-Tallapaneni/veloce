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


async def _run(lines: list[dict]) -> MCPServer:
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
    return server


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


async def test_a_never_settling_task_is_reclaimed_on_eof():
    """The defect: this task and its runner survived the connection."""
    server = await _run(
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
    server = await _run(
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
    """The reclaim must not disturb the ordinary path."""
    server = await _run(
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
    assert server._tasks.tasks == {}


async def test_the_connection_sink_is_still_unregistered():
    """Reclaiming tasks must not replace the unregister it sits beside."""
    server = await _run([_INIT])
    registry = server._connections
    assert registry is None or not registry._sinks
