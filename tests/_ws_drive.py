"""Drive one WebSocket connection through an app's ASGI surface.

Three modules carried this as a private `_run_ws`, and a fourth copy sat inline
inside a test in the module that already defined one 77 lines above it. Two of
the three were identical apart from a docstring; the third had grown a
`query_string` parameter the others lacked, so a test needing it had to know
which module's copy to look at.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from veloce import Veloce


def run_ws(
    app: Veloce,
    path: str,
    query_string: bytes = b"",
    *,
    raises: type[BaseException] | None = None,
) -> list[dict[str, Any]]:
    """Connect to `path` and return every message the app sent.

    The client connects, then goes away: after the initial `websocket.connect`,
    `receive` answers `websocket.disconnect`. That is what a test asserting on
    the handshake, on a refusal, or on the close frame wants.

    `raises` is for a handler expected to fail: the exception is caught here and
    the messages sent before it are still returned, which is why the fourth copy
    of this driver was written inline - a function that returns its result
    cannot also be wrapped in `pytest.raises`.
    """
    scope = {
        "type": "websocket",
        "path": path,
        "headers": [],
        "query_string": query_string,
    }
    incoming = [{"type": "websocket.connect"}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if incoming:
            return incoming.pop(0)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    loop = asyncio.new_event_loop()
    try:
        if raises is None:
            loop.run_until_complete(app(scope, receive, send))
        else:
            with pytest.raises(raises):
                loop.run_until_complete(app(scope, receive, send))
    finally:
        loop.close()
    return sent
