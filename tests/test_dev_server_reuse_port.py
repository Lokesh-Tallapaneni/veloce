"""The native dev server only requests SO_REUSEPORT where the OS supports it.

`reuse_port=True` on a platform without `SO_REUSEPORT` (Windows) makes the stdlib
selector event loop raise `ValueError: reuse_port not supported by socket module`,
which kills the serving thread before it binds. `_serve` must gate the option on
`hasattr(socket, "SO_REUSEPORT")` so the native server starts everywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

from veloce import Veloce


class _FakeServer:
    """Stand-in for the object `loop.create_server` returns."""

    async def __aenter__(self) -> _FakeServer:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def close(self) -> None:
        pass


async def _capture_reuse_port() -> object:
    """Run `_serve` to the (faked) `create_server` call and capture `reuse_port`."""
    app = Veloce(openapi_url=None)
    loop = asyncio.get_running_loop()
    captured: dict[str, object] = {}

    async def fake_create_server(*args: object, **kwargs: object) -> _FakeServer:
        captured["reuse_port"] = kwargs.get("reuse_port", "<unset>")
        return _FakeServer()

    loop.create_server = fake_create_server  # type: ignore[method-assign]

    task = asyncio.create_task(app._serve("127.0.0.1", 0, None))
    try:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if "reuse_port" in captured:
                break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return captured.get("reuse_port", "<never-called>")


async def test_reuse_port_gated_on_socket_support():
    # True where SO_REUSEPORT exists (Linux/BSD); None where it does not
    # (Windows) so `create_server` does not raise and the server still binds.
    expected = True if hasattr(socket, "SO_REUSEPORT") else None
    assert (await _capture_reuse_port()) is expected
