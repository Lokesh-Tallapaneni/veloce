"""app.lifespan_context() — separable lifespan manager (T6)."""

from __future__ import annotations

import pytest

from veloce import Veloce


@pytest.mark.asyncio
async def test_lifespan_context_runs_startup_and_shutdown():
    app = Veloce()
    events: list[str] = []

    @app.on_event("startup")
    async def on_start():
        events.append("startup")

    @app.on_event("shutdown")
    async def on_stop():
        events.append("shutdown")

    async with app.lifespan_context():
        assert events == ["startup"]
    assert events == ["startup", "shutdown"]


@pytest.mark.asyncio
async def test_lifespan_context_yields_the_app():
    app = Veloce()
    async with app.lifespan_context() as bound:
        assert bound is app


@pytest.mark.asyncio
async def test_lifespan_context_runs_lifespan_cm():
    import contextlib

    order: list[str] = []

    @contextlib.asynccontextmanager
    async def lifespan(app):
        order.append("enter")
        yield
        order.append("exit")

    app = Veloce(lifespan=lifespan)
    async with app.lifespan_context():
        assert order == ["enter"]
    assert order == ["enter", "exit"]


@pytest.mark.asyncio
async def test_lifespan_context_double_enter_raises():
    app = Veloce()
    mgr = app.lifespan_context()
    async with mgr:
        with pytest.raises(RuntimeError, match="already entered"):
            await mgr.__aenter__()


@pytest.mark.asyncio
async def test_lifespan_context_reusable_after_exit():
    app = Veloce()
    count: list[int] = []

    @app.on_event("startup")
    async def s():
        count.append(1)

    async with app.lifespan_context():
        pass
    async with app.lifespan_context():
        pass
    assert len(count) == 2


class TestLifespan:
    @pytest.mark.asyncio
    async def test_lifespan_startup_shutdown(self):
        log = []

        async def lifespan(app):
            log.append("startup")
            app.state["db"] = {"connected": True}
            yield
            log.append("shutdown")

        from contextlib import asynccontextmanager

        app = Veloce(lifespan=asynccontextmanager(lifespan), openapi_url=None)

        await app._run_lifecycle("startup")
        assert "startup" in log
        assert app.state["db"]["connected"] is True

        await app._run_lifecycle("shutdown")
        assert "shutdown" in log
