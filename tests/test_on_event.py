"""@app.on_event / @app.on_startup lifecycle decorators."""

from __future__ import annotations

from veloce import Veloce


class TestOnEventDecorators:
    """Test startup/shutdown event decorators."""

    async def test_on_event_startup(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.on_event("startup")
        async def startup():
            log.append("started")

        await app._run_lifecycle("startup")
        assert "started" in log

    async def test_on_event_shutdown(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.on_event("shutdown")
        async def shutdown():
            log.append("stopped")

        await app._run_lifecycle("shutdown")
        assert "stopped" in log

    async def test_on_startup_decorator(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.on_startup
        async def init_db():
            log.append("db_ready")

        await app._run_lifecycle("startup")
        assert "db_ready" in log
