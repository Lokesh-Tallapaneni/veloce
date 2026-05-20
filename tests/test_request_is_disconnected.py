"""Request.is_disconnected() — compatible shim."""

from __future__ import annotations

import pytest

from veloce import Request


def _req() -> Request:
    return Request(method="GET", path="/", query_string="", headers={}, body=b"")


@pytest.mark.asyncio
async def test_is_disconnected_returns_false():
    # Body is fully buffered before dispatch — never disconnected.
    assert await _req().is_disconnected() is False


@pytest.mark.asyncio
async def test_is_disconnected_is_awaitable():
    coro = _req().is_disconnected()
    result = await coro
    assert result is False


@pytest.mark.asyncio
async def test_is_disconnected_usable_in_handler_poll_pattern():
    """the ASGI convention handlers poll it in a loop — must terminate immediately."""
    req = _req()
    polls = 0
    while not await req.is_disconnected() and polls < 3:
        polls += 1
    assert polls == 3
