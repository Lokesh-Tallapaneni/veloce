"""The native dev server only requests SO_REUSEPORT where the OS supports it.

`reuse_port=True` on a platform without `SO_REUSEPORT` (Windows) makes the stdlib
selector event loop raise `ValueError: reuse_port not supported by socket module`,
which kills the serving thread before it binds. `_serve` must gate the option on
`hasattr(socket, "SO_REUSEPORT")` so the native server starts everywhere.
"""

from __future__ import annotations

import socket

from tests._dev_server import BindProbe, cancel, serve_until_bound
from veloce import Veloce


async def _capture_reuse_port(monkeypatch) -> object:
    """Run `_serve` to the (faked) `create_server` call and capture `reuse_port`."""
    probe = BindProbe().install(monkeypatch)
    task = await serve_until_bound(Veloce(openapi_url=None), probe)
    await cancel(task)
    return probe.kwargs.get("reuse_port", "<unset>")


async def test_reuse_port_gated_on_socket_support(monkeypatch):
    # True where SO_REUSEPORT exists (Linux/BSD); None where it does not
    # (Windows) so `create_server` does not raise and the server still binds.
    expected = True if hasattr(socket, "SO_REUSEPORT") else None
    assert (await _capture_reuse_port(monkeypatch)) is expected
