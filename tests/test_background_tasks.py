"""BackgroundTasks dependency injection into handlers."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import BackgroundTasks, Request, Veloce


class TestBackgroundTasksInjection:
    @pytest.mark.asyncio
    async def test_background_tasks_injected(self):
        app = Veloce(openapi_url=None)
        results = []

        async def bg_work(val: str):
            results.append(val)

        @app.post("/work")
        async def do_work(request: Request, tasks: BackgroundTasks):
            tasks.add_task(bg_work, "done")
            return {"status": "queued"}

        resp = await app.handle_request(make_request(method="POST", path="/work"))
        assert resp.status_code == 200
        # Background tasks are scheduled via create_task
        import asyncio

        await asyncio.sleep(0.05)
        assert "done" in results
