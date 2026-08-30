"""A recording transport for driving `HttpProtocol` in memory.

`test_server_protocol.py` defined it and three of its concerns used it; two of
those are now their own modules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from veloce.serving.protocol import HttpProtocol


class _FakeTransport(asyncio.Transport):
    """Minimal asyncio.Transport stand-in for protocol unit tests."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.closed = False
        # Flow-control state + call tallies so backpressure tests can assert
        # pause_reading / resume_reading actually fired.
        self.reading_paused = False
        self.pause_reading_calls = 0
        self.resume_reading_calls = 0

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def get_extra_info(self, name: str, default: object = None) -> object:
        """Return `default` for every key, as an unconnected transport would.

        Six modules forked this class for want of this one method. The two that
        answer a key for real - a `peername`, or a recording variant - keep
        their own, because answering differently is the thing they test.
        """
        return default

    def pause_reading(self) -> None:
        self.reading_paused = True
        self.pause_reading_calls += 1

    def resume_reading(self) -> None:
        self.reading_paused = False
        self.resume_reading_calls += 1


def _drain_loop(loop: asyncio.AbstractEventLoop, proto: HttpProtocol) -> None:
    """Run the event loop until the connection's server loop finishes."""
    task = proto._server_loop
    if task is not None:
        loop.run_until_complete(task)


def _run_until(
    loop: asyncio.AbstractEventLoop,
    predicate: Callable[[], bool],
    *,
    max_turns: int = 100,
) -> None:
    """Drive the loop one scheduling turn at a time until `predicate` holds.

    Lets a parked continuation make progress without depending on the exact
    number of turns a given Python version needs — the loop advances until the
    observable condition is reached (or `max_turns` is exhausted, which fails
    the caller's subsequent assertion rather than hanging)."""
    for _ in range(max_turns):
        if predicate():
            return
        loop.run_until_complete(asyncio.sleep(0))
