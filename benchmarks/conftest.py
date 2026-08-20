"""Shared helpers for the CodSpeed benchmark suite.

Kept separate from `tests/conftest.py`: the benchmarks are collected on
their own (`pytest benchmarks`) and must not inherit the Hypothesis
profiles or the app fixtures the test suite installs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

import pytest

from veloce import Request

_T = TypeVar("_T")

# One event loop for the whole benchmark session. Veloce's hot paths are
# coroutines, so every measured callable has to drive a loop; creating one
# per iteration (what `asyncio.run` does) would bury the code under test in
# loop setup and teardown noise.
_loop = asyncio.new_event_loop()


def run_async(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run `coro` to completion on the shared benchmark event loop."""
    return _loop.run_until_complete(coro)


def make_request(
    method: str = "GET",
    path: str = "/",
    headers: dict[str, str] | list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
    query_string: str = "",
) -> Request:
    """Build a synthetic `Request`, the same shape dispatch receives."""
    return Request(
        method=method,
        path=path,
        query_string=query_string,
        headers=headers if headers is not None else {},
        body=body,
    )


@pytest.fixture(scope="session", autouse=True)
def _close_shared_loop():
    """Close the shared loop once the session ends."""
    yield
    _loop.close()
