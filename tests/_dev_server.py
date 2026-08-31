"""Shared stand-ins for driving `Veloce._serve` without opening a socket.

Three modules covering the same function each defined their own `_FakeServer`
and their own `for _ in range(N): await asyncio.sleep(0.01)` loop, and two
replaced `loop.create_server` by raw attribute assignment rather than through
`monkeypatch` - so a failure between the assignment and the `finally` left the
running loop's `create_server` permanently replaced, which is the sort of thing
that shows up as an unrelated test failing later.

The poll loops are the other half. Each slept *before* checking, so a harness
that would have been ready immediately still paid 10ms - about 40ms across the
four of them, which is what the change measures back (1.04s to 1.00s for the
three modules). The size of that is not the point: a fixed sleep with a
50-or-100 iteration ceiling is a wall-clock gamble, and the ceiling is what a
loaded CI machine loses against. Waiting on an `asyncio.Event` the fake sets is
exact, and its `wait_for` failure says "`_serve` never reached `create_server`"
instead of asserting on a value that was never captured.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any


class FakeServer:
    """Stand-in for the object `loop.create_server` returns.

    Records `close()` and `wait_closed()` so a test can assert the server was
    actually shut down, and supports `async with` because `_serve` uses it as a
    context manager.
    """

    def __init__(self) -> None:
        self.closed = False
        self.waited = False

    async def __aenter__(self) -> FakeServer:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


class BindProbe:
    """Captures the `create_server` call `_serve` makes, and when it happens.

    `server` is what the call returned, `kwargs` what it was passed, and
    `bound` is set the moment it lands - so a test waits for the event rather
    than sleeping in the hope that it has.
    """

    def __init__(self, server: FakeServer | None = None) -> None:
        self.server = server if server is not None else FakeServer()
        self.kwargs: dict[str, Any] = {}
        self.args: tuple[Any, ...] = ()
        self.bound = asyncio.Event()
        self.calls = 0

    async def create_server(self, *args: Any, **kwargs: Any) -> FakeServer:
        self.calls += 1
        self.args = args
        self.kwargs = kwargs
        self.bound.set()
        return self.server

    def install(self, monkeypatch, loop: asyncio.AbstractEventLoop | None = None) -> BindProbe:
        """Replace `loop.create_server` for the duration of the test.

        Through `monkeypatch`, so it is restored even when the test fails part
        way - two of the modules this replaces assigned the attribute directly
        and undid it in a `finally` that a failure could skip.
        """
        loop = loop or asyncio.get_running_loop()
        monkeypatch.setattr(loop, "create_server", self.create_server)
        return self


async def serve_until_bound(
    app: Any,
    probe: BindProbe,
    host: str = "127.0.0.1",
    port: int = 0,
    ssl_context: Any = None,
    *,
    timeout: float = 5.0,
) -> asyncio.Task:
    """Start `app._serve` and return once it has called `create_server`.

    The returned task is still running; the caller owns cancelling it, or
    driving it to completion through whatever it is testing.
    """
    task = asyncio.create_task(app._serve(host, port, ssl_context))
    try:
        await asyncio.wait_for(probe.bound.wait(), timeout)
    except (TimeoutError, asyncio.TimeoutError):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise AssertionError("`_serve` never reached `create_server`") from None
    return task


async def cancel(task: asyncio.Task) -> None:
    """Cancel a task started by `serve_until_bound` and await its unwind."""
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
