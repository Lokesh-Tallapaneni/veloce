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

**These tests previously proved neither half.** All three sent
`accept: application/json`, and `transports/http.py` selects the SSE path only
when the request accepts `text/event-stream`, so the code the module is named
for never ran. None dropped a client, and none observed a queue. Removing the
`draining[0]` guard in `send` - restoring the exact unbounded-queue leak this
module exists to prevent - left all three green.

So the queue is now observed directly. `_Post` opens a real streaming POST over
a raw ASGI scope and `hang_up()` drops the client mid-call, which is what tears
the SSE generator down.
"""

from __future__ import annotations

import asyncio

import pytest

from tests._mcp import HANDSHAKE_REVISION, PostStream
from veloce import MCPContext, Veloce

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": HANDSHAKE_REVISION,
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}

_CALL = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "chatty", "arguments": {}},
}


class _Recorder:
    """Counts what the transport hands to the SSE queue.

    The queue itself is a local inside the transport, so it is observed where it
    is filled: `asyncio.Queue.put` is wrapped for the duration of the test. That
    is the quantity the `draining[0]` guard controls, and the one no previous
    test looked at.
    """

    def __init__(self) -> None:
        self.puts = 0
        self._real = asyncio.Queue.put

    def install(self, monkeypatch) -> None:
        recorder = self

        async def counting_put(queue, item):
            recorder.puts += 1
            await recorder._real(queue, item)

        monkeypatch.setattr(asyncio.Queue, "put", counting_put)


def _app(finished: list[str], gate: asyncio.Event, steps: int = 20) -> Veloce:
    app = Veloce(title="SSE", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Emits several notifications, then finishes")
    async def chatty(ctx: MCPContext) -> str:
        await gate.wait()
        for index in range(steps):
            await ctx.log("info", f"step {index}")
        finished.append("done")
        return "ok"

    app.mount_mcp(transport="http", path="/mcp")
    return app


async def _initialise(app: Veloce) -> None:
    """Complete the handshake, and fail here if it did not.

    This tolerated `status is None` - the POST never reaching
    `http.response.start` at all - so the precondition every test in the module
    rests on could silently not hold, and the failure surfaced as an unrelated
    assertion further on. Entering the context now waits for the status, so
    there is one to assert.
    """
    async with PostStream(app, _INIT, revision=HANDSHAKE_REVISION) as post:
        assert post.status == 200, post.chunks


async def _until(ready, *, turns: int = 600) -> None:
    """Yield the loop until `ready()` holds.

    Four sites polled the same way with four different budgets - 200, 400, 200,
    600 - none of which said what it was sized for. The condition is what each
    was waiting on; the budget only has to be large enough not to expire, so
    there is one, and it is the largest that was in use.
    """
    for _ in range(turns):
        if ready():
            return
        await asyncio.sleep(0)


async def _pump_until(post: PostStream, needle: str, turns: int = 2000) -> str:
    """Give the loop turns until `needle` appears in the stream, then return it.

    Waiting on `post.chunks` being non-empty is not enough: the stream opens with
    a priming event, so the first chunk arrives long before the tool result does.
    """
    for _ in range(turns):
        payload = b"".join(post.chunks).decode()
        if needle in payload:
            return payload
        await asyncio.sleep(0)
    return b"".join(post.chunks).decode()


# ── the half about the call ──────────────────────────────────────────


async def test_the_call_still_completes_after_the_client_drops():
    """Disconnection is not cancellation - that contract must not regress."""
    finished: list[str] = []
    gate = asyncio.Event()
    app = _app(finished, gate)
    await _initialise(app)

    async with PostStream(app, _CALL, revision=HANDSHAKE_REVISION) as post:
        await post.hang_up()
        gate.set()
        await _until(lambda: bool(finished))

    assert finished == ["done"]


async def test_the_call_completes_even_though_nothing_reads_the_stream():
    finished: list[str] = []
    gate = asyncio.Event()
    app = _app(finished, gate, steps=50)
    await _initialise(app)

    async with PostStream(app, _CALL, revision=HANDSHAKE_REVISION) as post:
        await post.hang_up()
        gate.set()
        await _until(lambda: bool(finished))

    assert finished == ["done"]


# ── the half about the queue, which nothing observed before ──────────


async def test_notifications_are_not_queued_after_the_client_drops(monkeypatch):
    """The leak this module exists to prevent, measured.

    Twenty notifications are emitted *after* teardown. With the `draining[0]`
    guard removed they all reach the queue; with it in place none does.
    """
    finished: list[str] = []
    gate = asyncio.Event()
    app = _app(finished, gate, steps=20)
    await _initialise(app)

    recorder = _Recorder()
    async with PostStream(app, _CALL, revision=HANDSHAKE_REVISION) as post:
        await post.hang_up()
        recorder.install(monkeypatch)
        gate.set()
        await _until(lambda: bool(finished))

    assert finished == ["done"]
    assert recorder.puts == 0, (
        f"{recorder.puts} payloads were queued for a stream nobody is reading - "
        "the unbounded-buffer leak this module exists to prevent"
    )


async def test_more_notifications_after_teardown_still_queue_nothing(monkeypatch):
    """The count must not scale with the tool's chattiness."""
    finished: list[str] = []
    gate = asyncio.Event()
    app = _app(finished, gate, steps=100)
    await _initialise(app)

    recorder = _Recorder()
    async with PostStream(app, _CALL, revision=HANDSHAKE_REVISION) as post:
        await post.hang_up()
        recorder.install(monkeypatch)
        gate.set()
        await _until(lambda: bool(finished))

    assert finished == ["done"]
    assert recorder.puts == 0


# ── and a client that stays still gets its answer ────────────────────
#
# The negative. A "fix" that dropped the hand-off unconditionally would pass
# every assertion above and deliver nothing to anyone.


async def test_the_response_still_reaches_a_client_that_stayed():
    finished: list[str] = []
    gate = asyncio.Event()
    app = _app(finished, gate, steps=3)
    await _initialise(app)

    async with PostStream(app, _CALL, revision=HANDSHAKE_REVISION) as post:
        gate.set()
        payload = await _pump_until(post, "ok")

    assert finished == ["done"]
    assert "ok" in payload, payload


async def test_a_client_that_stayed_receives_the_notifications():
    """The other direction: the notifications a live stream *should* see."""
    finished: list[str] = []
    gate = asyncio.Event()
    app = _app(finished, gate, steps=3)
    await _initialise(app)

    async with PostStream(app, _CALL, revision=HANDSHAKE_REVISION) as post:
        gate.set()
        payload = await _pump_until(post, "step 0")

    assert "step 0" in payload, payload


async def test_the_stream_uses_the_sse_content_type():
    """The premise of every test above: the SSE branch is the one running.

    The previous version of this module sent `accept: application/json`, so this
    assertion would have failed - which is the whole reason it is here.
    """
    finished: list[str] = []
    gate = asyncio.Event()
    app = _app(finished, gate, steps=1)
    await _initialise(app)

    async with PostStream(app, _CALL, revision=HANDSHAKE_REVISION) as post:
        gate.set()
        payload = await _pump_until(post, "data:")

    assert "data:" in payload, payload


@pytest.mark.parametrize("steps", [1, 5])
async def test_the_tool_result_is_delivered_whatever_the_chattiness(steps):
    finished: list[str] = []
    gate = asyncio.Event()
    app = _app(finished, gate, steps=steps)
    await _initialise(app)

    async with PostStream(app, _CALL, revision=HANDSHAKE_REVISION) as post:
        gate.set()
        payload = await _pump_until(post, "ok")

    assert finished == ["done"]
    assert "ok" in payload, payload
