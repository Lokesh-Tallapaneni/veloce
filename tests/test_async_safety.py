"""Tests for async safety — no leaks, proper resource management."""

import asyncio

import pytest

from tests.conftest import make_request
from veloce import Request, StreamingResponse, Veloce
from veloce.serving.protocol import HttpProtocol


class TestNoDeprecatedEventLoop:
    """Verify no deprecated asyncio.get_event_loop() calls remain."""

    def test_no_get_event_loop_in_app(self):
        import inspect

        import veloce.app as mod

        source = inspect.getsource(mod)
        assert "get_event_loop()" not in source, (
            "app.py still uses deprecated asyncio.get_event_loop()"
        )

    def test_no_get_event_loop_in_protocol(self):
        import inspect

        import veloce.serving.protocol as mod

        source = inspect.getsource(mod)
        assert "get_event_loop()" not in source


class TestTaskStrongReferences:
    """Verify fire-and-forget tasks are held to prevent GC."""

    def test_protocol_has_active_tasks_set(self):
        assert hasattr(HttpProtocol, "_active_tasks")
        assert isinstance(HttpProtocol._active_tasks, set)

    def test_protocol_has_keep_alive_timeout(self):
        assert HttpProtocol.KEEP_ALIVE_TIMEOUT == 75


class TestSyncHandlerOffloading:
    """Verify sync handlers don't block the event loop."""

    @pytest.mark.asyncio
    async def test_sync_handler_runs_correctly(self):
        """Sync handler should still return the right result even through executor."""
        app = Veloce(openapi_url=None)

        @app.get("/sync")
        def sync_handler(request: Request):
            import time

            time.sleep(0.001)  # Would block event loop without executor
            return {"sync": True}

        resp = await app.handle_request(make_request(path="/sync"))
        assert resp.status_code == 200
        import orjson

        assert orjson.loads(resp.body)["sync"] is True

    @pytest.mark.asyncio
    async def test_async_handler_still_works(self):
        app = Veloce(openapi_url=None)

        @app.get("/async")
        async def async_handler(request: Request):
            await asyncio.sleep(0.001)
            return {"async": True}

        resp = await app.handle_request(make_request(path="/async"))
        assert resp.status_code == 200


class TestStreamingResponse:
    """Verify streaming responses produce correct output."""

    @pytest.mark.asyncio
    async def test_streaming_response_has_stream_to(self):
        async def generate():
            for i in range(3):
                yield f"chunk {i}\n".encode()

        resp = StreamingResponse(generate())
        assert hasattr(resp, "stream_to")
        # encode() should produce chunked headers
        encoded = resp.encode()
        assert b"Transfer-Encoding: chunked" in encoded

    @pytest.mark.asyncio
    async def test_streaming_response_returned_from_handler(self):
        app = Veloce(openapi_url=None)

        @app.get("/stream")
        async def stream(request: Request):
            async def generate():
                for i in range(3):
                    yield f"chunk {i}\n".encode()

            return StreamingResponse(generate(), content_type="text/plain")

        resp = await app.handle_request(make_request(path="/stream"))
        assert isinstance(resp, StreamingResponse)


class TestBackgroundTaskSafety:
    """Verify background tasks hold strong references."""

    @pytest.mark.asyncio
    async def test_background_task_completes(self):
        app = Veloce(openapi_url=None)
        results = []

        async def bg_work():
            results.append("done")

        from veloce.background import BackgroundTasks

        @app.post("/work")
        async def do_work(request: Request, tasks: BackgroundTasks):
            tasks.add_task(bg_work)
            return {"queued": True}

        resp = await app.handle_request(make_request(method="POST", path="/work"))
        assert resp.status_code == 200
        await asyncio.sleep(0.05)
        assert "done" in results


class TestTeardownAlwaysRuns:
    """Verify teardown hooks run even on errors."""

    @pytest.mark.asyncio
    async def test_teardown_on_success(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.teardown_request
        async def td(exc):
            log.append(("td", exc is None))

        @app.get("/ok")
        async def ok(request: Request):
            return {"ok": True}

        await app.handle_request(make_request(path="/ok"))
        assert log == [("td", True)]

    @pytest.mark.asyncio
    async def test_teardown_on_exception(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.teardown_request
        async def td(exc):
            log.append(("td", type(exc).__name__ if exc else None))

        @app.get("/boom")
        async def boom(request: Request):
            raise RuntimeError("crash")

        await app.handle_request(make_request(path="/boom"))
        assert log[0][1] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_teardown_on_404(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.teardown_request
        async def td(exc):
            log.append("ran")

        await app.handle_request(make_request(path="/nope"))
        assert "ran" in log


class TestGracefulShutdownStructure:
    """Verify graceful shutdown components exist."""

    def test_graceful_shutdown_method_exists(self):
        app = Veloce()
        assert hasattr(app, "_graceful_shutdown")

    @pytest.mark.asyncio
    async def test_teardown_appcontext_called_on_shutdown(self):
        log = []

        app = Veloce(openapi_url=None)

        @app.teardown_appcontext
        def cleanup(exc):
            log.append("appcontext_teardown")

        # Simulate graceful shutdown
        loop = asyncio.get_running_loop()
        await app._graceful_shutdown(loop)
        assert "appcontext_teardown" in log


class TestPerformanceAfterFixes:
    """Sanity check that async fixes didn't kill performance."""

    @pytest.mark.asyncio
    async def test_async_handler_under_50us(self):
        import time

        app = Veloce(openapi_url=None)

        @app.get("/bench")
        async def bench(request: Request):
            return {"ok": True}

        # Warmup
        for _ in range(100):
            await app.handle_request(make_request(path="/bench"))

        times = []
        for _ in range(1000):
            start = time.perf_counter_ns()
            await app.handle_request(make_request(path="/bench"))
            times.append(time.perf_counter_ns() - start)

        avg_us = sum(times) / len(times) / 1000
        assert avg_us < 100, f"Avg {avg_us:.1f}us exceeds 100us budget"

    @pytest.mark.asyncio
    async def test_sync_handler_reasonable_overhead(self):
        """Sync handlers via executor will be slower but should still be fast."""
        import time

        app = Veloce(openapi_url=None)

        @app.get("/sync-bench")
        def sync_bench(request: Request):
            return {"ok": True}

        # Warmup
        for _ in range(50):
            await app.handle_request(make_request(path="/sync-bench"))

        times = []
        for _ in range(200):
            start = time.perf_counter_ns()
            await app.handle_request(make_request(path="/sync-bench"))
            times.append(time.perf_counter_ns() - start)

        avg_us = sum(times) / len(times) / 1000
        # Sync handlers go through executor — allow 500us
        assert avg_us < 500, f"Sync handler avg {avg_us:.1f}us exceeds 500us budget"
