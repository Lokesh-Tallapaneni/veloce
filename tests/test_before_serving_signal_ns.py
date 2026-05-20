"""before_serving / after_serving + app.signal_namespace."""

from __future__ import annotations

import pytest

from veloce import Veloce


@pytest.mark.asyncio
async def test_before_serving_runs_on_startup():
    app = Veloce(openapi_url=None)
    fired: list[str] = []

    @app.before_serving
    async def init():
        fired.append("started")

    await app._run_lifecycle("startup")
    assert fired == ["started"]


@pytest.mark.asyncio
async def test_after_serving_runs_on_shutdown():
    app = Veloce(openapi_url=None)
    fired: list[str] = []

    @app.after_serving
    async def cleanup():
        fired.append("stopped")

    await app._run_lifecycle("shutdown")
    assert fired == ["stopped"]


@pytest.mark.asyncio
async def test_before_serving_alongside_on_startup_both_fire():
    """Both decorators register on the same `_on_startup` list."""
    app = Veloce(openapi_url=None)
    fired: list[str] = []

    @app.before_serving
    def a():
        fired.append("a")

    @app.on_startup
    def b():
        fired.append("b")

    await app._run_lifecycle("startup")
    assert fired == ["a", "b"]


def test_signal_namespace_exposes_module():
    """`app.signal_namespace` is the `veloce.signals` module."""
    from veloce import signals

    app = Veloce(openapi_url=None)
    assert app.signal_namespace is signals
    # Standard signals reachable through it.
    assert app.signal_namespace.request_started is signals.request_started
