"""One rendezvous pair for the dependency-concurrency test modules.

Proving that two dependencies run concurrently used to mean sampling
`time.monotonic()` around real `asyncio.sleep` calls and asserting a threshold.
That is a wall-clock test in the default suite - the class this project excludes
behind the `perf` marker (`addopts = ["-m", "not perf"]`) - and it fails under a
loaded runner for reasons unrelated to the code.

A rendezvous proves the same thing structurally: each dependency records its
arrival and then waits for the other, so a sequential resolver cannot get past
the first one and the request fails outright rather than passing slowly. There
is no threshold to tune and nothing to be flaky about.

Kept as a helper module rather than a `conftest.py` fixture because callers want
the two dependency callables to close over their own state, one pair per test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

_Dep = Callable[[], Coroutine[Any, Any, str]]


def rendezvous_pair(timeout: float = 5.0) -> tuple[_Dep, _Dep, list[str], asyncio.Event]:
    """Two dependencies that can only both finish if they run concurrently.

    Returns the two dependency callables, the list they record arrivals into,
    and the event that is set once both have arrived.

    `asyncio.Barrier` says this in one line but landed in 3.11; this project
    supports 3.10.
    """
    arrived: list[str] = []
    both_here = asyncio.Event()

    async def _arrive(name: str) -> str:
        arrived.append(name)
        if len(arrived) == 2:
            both_here.set()
        await asyncio.wait_for(both_here.wait(), timeout=timeout)
        return name

    async def slow_a() -> str:
        return await _arrive("a")

    async def slow_b() -> str:
        return await _arrive("b")

    return slow_a, slow_b, arrived, both_here
