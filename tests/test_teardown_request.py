"""@app.teardown_request hooks — run after success, error, and 404."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce


class TestTeardownRequest:
    @pytest.mark.asyncio
    async def test_teardown_runs_after_success(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.teardown_request
        def on_teardown(exc):
            log.append(("teardown", exc))

        @app.get("/ok")
        async def ok(request: Request):
            return {"ok": True}

        await app.handle_request(make_request(path="/ok"))
        assert len(log) == 1
        assert log[0] == ("teardown", None)

    @pytest.mark.asyncio
    async def test_teardown_runs_after_error(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.teardown_request
        def on_teardown(exc):
            log.append(("teardown", type(exc).__name__ if exc else None))

        @app.get("/crash")
        async def crash(request: Request):
            raise ValueError("boom")

        await app.handle_request(make_request(path="/crash"))
        assert len(log) == 1
        assert log[0][1] == "ValueError"

    @pytest.mark.asyncio
    async def test_teardown_runs_after_404(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.teardown_request
        def on_teardown(exc):
            log.append("teardown")

        await app.handle_request(make_request(path="/nonexistent"))
        assert "teardown" in log
