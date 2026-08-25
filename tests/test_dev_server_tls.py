"""F10 — the built-in dev server accepts an optional ssl_context.

`app.run(ssl_context=...)` hands the context straight to
`loop.create_server(ssl=...)`. With no context the serving path is
identical to plain HTTP — `ssl=None` is exactly `create_server`'s default.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect

from veloce import Veloce


class _FakeServer:
    """Minimal stand-in for the object `loop.create_server` returns."""

    async def __aenter__(self) -> _FakeServer:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def close(self) -> None:
        pass


async def _capture_serve_ssl(ssl_context: object) -> object:
    """Run `_serve` far enough to reach `create_server`, capturing its
    `ssl=` argument, then cancel."""
    app = Veloce(openapi_url=None)
    loop = asyncio.get_running_loop()
    captured: dict[str, object] = {}

    async def fake_create_server(*args: object, **kwargs: object) -> _FakeServer:
        captured["ssl"] = kwargs.get("ssl")
        return _FakeServer()

    loop.create_server = fake_create_server  # type: ignore[method-assign]

    task = asyncio.create_task(app._serve("127.0.0.1", 0, ssl_context))
    try:
        # Give `_serve` a moment to reach the (faked) create_server call.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if "ssl" in captured:
                break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return captured.get("ssl", "<never-called>")


# ── signature ─────────────────────────────────────────────────────────


def test_run_accepts_an_ssl_context_parameter():
    params = inspect.signature(Veloce.run).parameters
    assert "ssl_context" in params
    assert params["ssl_context"].default is None


# ── plumbing ──────────────────────────────────────────────────────────


async def test_ssl_context_is_forwarded_to_create_server():
    sentinel = object()  # stands in for an ssl.SSLContext
    assert (await _capture_serve_ssl(sentinel)) is sentinel


async def test_no_ssl_context_forwards_none():
    # The default path: `ssl=None` — `create_server` behaves as plain HTTP.
    assert (await _capture_serve_ssl(None)) is None
