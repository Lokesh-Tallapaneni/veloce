"""Two narrow lifetime defects in the stdio transport's ordering and correlation.

**The cancel (CR-12).** `_dispatch_in_order` waits on its predecessor under
`contextlib.suppress(BaseException)`, intending to ignore the *predecessor's*
failure - "a failure in the predecessor is its own business". But
`asyncio.CancelledError` derives from `BaseException`, not `Exception`, so a
cancel delivered to *this* task while it is parked on the shield is absorbed
too, and the task continues into `await self._dispatch(...)`.

That matters because `serve` cancels every in-flight task as it unwinds, then
unregisters the connection and evicts the session. A request that absorbed its
own cancel dispatches afterwards, against a reclaimed session whose
notifications have nowhere to go.

**The leak (CR-13).** `request()` registers its future in `self._pending`
*before* awaiting `_emit`, and the `try/finally` that pops it opens after. When
`_emit` raises - `encode_envelope` raises `TypeError` for a value no encoder can
represent, reachable because `ctx.elicit()` / `ctx.sample()` put author-supplied
`params` straight into that envelope - the exception reaches the tool correctly
and the entry stays for the process lifetime. `_fail_pending` at EOF then settles
a future nobody is awaiting.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce import Veloce
from veloce.contrib.mcp import MCPServer
from veloce.contrib.mcp.session import MCPSession
from veloce.contrib.mcp.transports.stdio import StdioTransport


class _Recording(StdioTransport):
    """Records what reached `_dispatch`, and what `_emit` was asked to send.

    A subclass rather than monkeypatching: `StdioTransport` is slotted, so its
    methods are read-only on an instance. `emit_error` makes `_emit` raise, which
    is the `encode_envelope` `TypeError` these tests are about.
    """

    __slots__ = ("dispatched", "sent", "emit_error")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.dispatched: list[str] = []
        self.sent: list[dict] = []
        self.emit_error: Exception | None = None

    async def _dispatch(self, message, session) -> None:
        self.dispatched.append(message["method"])

    async def _emit(self, message) -> None:
        if self.emit_error is not None:
            raise self.emit_error
        self.sent.append(message)


def _transport() -> _Recording:
    """A transport over streams that are never driven; these tests call it directly."""
    app = Veloce(title="T", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    async def read_line() -> bytes:
        return b""

    async def write_line(data: bytes) -> None:
        return None

    return _Recording(MCPServer(app), read_line, write_line)


# ── CR-12: the suppress must not absorb this task's own cancel ───────


def test_cancelled_error_is_not_an_exception_subclass():
    """The premise, stated once so the tests below are not mysterious."""
    assert issubclass(asyncio.CancelledError, BaseException)
    assert not issubclass(asyncio.CancelledError, Exception)


async def test_a_cancel_while_waiting_on_the_predecessor_is_not_absorbed():
    """The regression: the task continued into dispatch after being cancelled."""
    transport = _transport()
    session = MCPSession(persistent=False)

    async def never() -> None:
        await asyncio.Event().wait()

    predecessor = asyncio.ensure_future(never())

    waiter = asyncio.ensure_future(
        transport._dispatch_in_order(
            predecessor, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, session
        )
    )
    await asyncio.sleep(0)
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert transport.dispatched == [], "a cancelled request dispatched against a reclaimed session"
    predecessor.cancel()


async def test_a_predecessors_failure_is_still_ignored():
    """The behaviour the suppress exists for, which the fix must keep."""
    transport = _transport()
    session = MCPSession(persistent=False)

    async def fails() -> None:
        raise RuntimeError("the predecessor's own business")

    predecessor = asyncio.ensure_future(fails())

    await transport._dispatch_in_order(
        predecessor, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, session
    )

    assert transport.dispatched == ["tools/list"]


async def test_a_predecessors_cancellation_is_still_ignored():
    """A cancelled predecessor is a failed predecessor, not a cancel of us."""
    transport = _transport()
    session = MCPSession(persistent=False)

    async def never() -> None:
        await asyncio.Event().wait()

    predecessor = asyncio.ensure_future(never())
    await asyncio.sleep(0)
    predecessor.cancel()

    await transport._dispatch_in_order(
        predecessor, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, session
    )

    assert transport.dispatched == ["tools/list"]


# ── CR-13: a failed emit leaves no correlation entry behind ──────────


async def test_a_failed_emit_leaves_no_pending_entry():
    """The regression: one leaked entry per failed issue, for the process life."""
    transport = _transport()

    transport.emit_error = TypeError("no encoder for this value")

    with pytest.raises(TypeError):
        await transport.request("sampling/createMessage", {"bad": object()})

    assert transport._pending == {}, "the correlation entry outlived the failed request"


async def test_a_successful_request_still_correlates():
    """The control: the entry must exist while the reply is outstanding."""
    transport = _transport()

    issued = asyncio.ensure_future(transport.request("roots/list", {}))
    await asyncio.sleep(0)

    assert len(transport._pending) == 1
    request_id = transport.sent[0]["id"]
    transport._pending[request_id].set_result({"roots": []})

    assert await issued == {"roots": []}
    assert transport._pending == {}


async def test_a_second_failed_emit_does_not_accumulate():
    """Stated as accumulation, because one leak is only visible as many."""
    transport = _transport()

    transport.emit_error = TypeError("no encoder for this value")

    for _ in range(5):
        with pytest.raises(TypeError):
            await transport.request("sampling/createMessage", {"bad": object()})

    assert transport._pending == {}
