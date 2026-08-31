"""F10 — the built-in dev server accepts an optional ssl_context.

`app.run(ssl_context=...)` hands the context straight to
`loop.create_server(ssl=...)`. With no context the serving path is
identical to plain HTTP — `ssl=None` is exactly `create_server`'s default.
"""

from __future__ import annotations

import inspect

from tests._dev_server import BindProbe, cancel, serve_until_bound
from veloce import Veloce


async def _capture_serve_ssl(monkeypatch, ssl_context: object) -> object:
    """Run `_serve` far enough to reach `create_server` and capture its `ssl=`."""
    probe = BindProbe().install(monkeypatch)
    task = await serve_until_bound(Veloce(openapi_url=None), probe, ssl_context=ssl_context)
    await cancel(task)
    return probe.kwargs.get("ssl", "<never-called>")


# ── signature ─────────────────────────────────────────────────────────


def test_run_accepts_an_ssl_context_parameter():
    params = inspect.signature(Veloce.run).parameters
    assert "ssl_context" in params
    assert params["ssl_context"].default is None


# ── plumbing ──────────────────────────────────────────────────────────


async def test_ssl_context_is_forwarded_to_create_server(monkeypatch):
    sentinel = object()  # stands in for an ssl.SSLContext
    assert (await _capture_serve_ssl(monkeypatch, sentinel)) is sentinel


async def test_no_ssl_context_forwards_none(monkeypatch):
    # The default path: `ssl=None` — `create_server` behaves as plain HTTP.
    assert (await _capture_serve_ssl(monkeypatch, None)) is None
