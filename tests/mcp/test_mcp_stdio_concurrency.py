"""The stdio loop keeps reading while a handler runs.

It used to await each dispatch before reading the next line. Everything the
spec puts on the connection to reach an *in-flight* request was therefore
unreachable: `notifications/cancelled` could only be read once the call it
cancels had already finished, and a liveness `ping` queued behind a slow tool.
The cancellation machinery itself was correct and worked over HTTP — nothing
could deliver the message.

Dispatching off the loop fixes that, and costs two things that have to be paid
for explicitly, both asserted here: ordinary requests must still execute in
arrival order (a client that sends `logging/setLevel` before a call expects the
level to be in force), and concurrent writers must not interleave halves of a
line, because the framing is one JSON message per line.
"""

from __future__ import annotations

import asyncio

import orjson

from veloce import MCPContext, Veloce
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.transports.stdio import StdioTransport

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
}


class _Driver:
    """Feed lines on demand and collect what the server writes."""

    def __init__(self, server: MCPServer) -> None:
        self.outbox: list[dict] = []
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.transport = StdioTransport(server, self._read, self._write)

    def feed(self, message: dict) -> None:
        self._queue.put_nowait(orjson.dumps(message))

    def close(self) -> None:
        self._queue.put_nowait(None)

    async def _read(self) -> bytes | None:
        return await self._queue.get()

    async def _write(self, data: bytes) -> None:
        self.outbox.append(orjson.loads(data))

    async def until(self, predicate, limit: float = 2.0) -> None:
        """Wait for something to appear in the outbox, without a fixed sleep."""
        deadline = asyncio.get_running_loop().time() + limit
        while not predicate(self.outbox):
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"condition not met; outbox={self.outbox}")
            await asyncio.sleep(0)


def _app(started: asyncio.Event, release: asyncio.Event) -> Veloce:
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Blocks until released")
    async def slow(ctx: MCPContext) -> str:
        started.set()
        await release.wait()
        return "finished"

    @app.mcp_tool(description="Returns at once")
    async def quick() -> str:
        return "quick"

    return app


# ── A running call can be reached ────────────────────────────────────


async def test_ping_is_answered_while_a_slow_tool_runs():
    """The plain liveness case: this used to queue behind the tool."""
    started, release = asyncio.Event(), asyncio.Event()
    driver = _Driver(MCPServer(_app(started, release)))
    serve = asyncio.ensure_future(driver.transport.serve())

    driver.feed(_INIT)
    driver.feed(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "slow", "arguments": {}},
        }
    )
    await asyncio.wait_for(started.wait(), 2)

    driver.feed({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    await driver.until(lambda out: any(m.get("id") == 3 for m in out))

    # The ping was answered while the tool was still parked.
    assert not release.is_set()
    release.set()
    driver.close()
    await asyncio.wait_for(serve, 2)


async def test_a_cancellation_reaches_a_call_that_is_still_running():
    """The defect this refactor exists for.

    Cancellation is not a flag the handler polls: the dispatch task is actually
    cancelled, so the handler sees `CancelledError`. What the serial loop broke
    was delivery — the notification could not be read until the call it named
    had already returned.
    """
    started = asyncio.Event()
    cancelled: list[str] = []
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Runs until cancelled")
    async def forever() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append("yes")
            raise
        return "never"

    driver = _Driver(MCPServer(app))
    serve = asyncio.ensure_future(driver.transport.serve())

    driver.feed(_INIT)
    driver.feed(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "forever", "arguments": {}},
        }
    )
    await asyncio.wait_for(started.wait(), 2)

    driver.feed({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}})
    for _ in range(200):
        if cancelled:
            break
        await asyncio.sleep(0)

    driver.close()
    await asyncio.wait_for(serve, 2)
    assert cancelled == ["yes"], "the cancellation never reached the running call"
    # A cancelled request expects no response frame.
    assert not [m for m in driver.outbox if m.get("id") == 2]


# ── What the refactor must not cost ──────────────────────────────────


async def test_ordinary_requests_keep_their_arrival_order():
    """A call must not overtake the `logging/setLevel` sent before it."""
    app = Veloce(openapi_url=None)
    seen: list[str] = []

    @app.mcp_tool(description="Records when it ran")
    async def first() -> str:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        seen.append("first")
        return "first"

    @app.mcp_tool(description="Records when it ran")
    async def second() -> str:
        seen.append("second")
        return "second"

    driver = _Driver(MCPServer(app))
    serve = asyncio.ensure_future(driver.transport.serve())
    driver.feed(_INIT)
    driver.feed(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "first", "arguments": {}},
        }
    )
    driver.feed(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "second", "arguments": {}},
        }
    )
    driver.close()
    await asyncio.wait_for(serve, 2)

    assert seen == ["first", "second"], "a later request overtook an earlier one"


async def test_the_log_level_set_by_an_earlier_request_applies_to_a_later_one():
    """`logging/setLevel` is connection state; concurrency must not lose it."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Logs below the threshold")
    async def noisy(ctx: MCPContext) -> str:
        await ctx.log("info", "noisy")
        return "ok"

    driver = _Driver(MCPServer(app))
    serve = asyncio.ensure_future(driver.transport.serve())
    driver.feed(_INIT)
    driver.feed(
        {"jsonrpc": "2.0", "id": 2, "method": "logging/setLevel", "params": {"level": "error"}}
    )
    driver.feed(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "noisy", "arguments": {}},
        }
    )
    driver.close()
    await asyncio.wait_for(serve, 2)

    assert [m for m in driver.outbox if m.get("method") == "notifications/message"] == []


async def test_every_written_line_is_one_whole_message():
    """Concurrent writers behind one lock; a torn line is unparseable."""
    app = Veloce(openapi_url=None)

    @app.mcp_tool(description="Emits several notifications")
    async def chatty(ctx: MCPContext) -> str:
        for index in range(10):
            await ctx.log("info", f"line {index}")
        return "ok"

    driver = _Driver(MCPServer(app))
    serve = asyncio.ensure_future(driver.transport.serve())
    driver.feed(_INIT)
    for request_id in (2, 3, 4):
        driver.feed(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "chatty", "arguments": {}},
            }
        )
    driver.close()
    await asyncio.wait_for(serve, 3)

    # Every collected entry decoded, which is only true if no write was torn.
    assert all(isinstance(m, dict) for m in driver.outbox)
    assert sum(1 for m in driver.outbox if m.get("id") in {2, 3, 4}) == 3


async def test_requests_already_running_are_answered_before_eof_returns():
    """EOF drains rather than discarding replies to requests the client sent."""
    started, release = asyncio.Event(), asyncio.Event()
    driver = _Driver(MCPServer(_app(started, release)))
    serve = asyncio.ensure_future(driver.transport.serve())

    driver.feed(_INIT)
    driver.feed(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "slow", "arguments": {}},
        }
    )
    await asyncio.wait_for(started.wait(), 2)
    driver.close()
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(serve, 2)

    answered = [m for m in driver.outbox if m.get("id") == 2]
    assert answered, "the reply was dropped when the client closed its write side"
    assert answered[0]["result"]["content"][0]["text"] == "finished"
