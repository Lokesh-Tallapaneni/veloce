"""A throwaway event loop for the tests that drive `HttpProtocol` directly.

`loop.close()` in a `finally` looks like teardown and is not. A task cancelled
without the loop running again never gets its done callbacks, and
`HttpProtocol._active_tasks` - a process-wide set - is pruned by exactly those
callbacks. Forty-four connections' server loops stayed in that set for the rest
of the session.

That cost more than tidiness. `Veloce._graceful_shutdown` waits on
`_active_tasks` with `GRACEFUL_DRAIN_TIMEOUT`, so the one later test that calls
it waited the full **thirty seconds** on tasks whose loop was gone - seventeen
percent of the suite's wall clock, in a test that finishes instantly on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator


@contextlib.contextmanager
def protocol_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A fresh loop, drained of its own tasks before it closes."""
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        drain(loop)
        loop.close()


def close_drained(loop: asyncio.AbstractEventLoop) -> None:
    """Drain `loop`, then close it. The teardown `loop.close()` looks like."""
    drain(loop)
    loop.close()


def drain(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel every task on `loop` and run it until they have all finished."""
    pending = asyncio.all_tasks(loop)
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
