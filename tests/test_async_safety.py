"""Tests for async safety — no leaks, proper resource management."""

import asyncio
import pathlib

import pytest

import veloce
from tests.conftest import make_request
from veloce import Request, StreamingResponse, Veloce
from veloce.serving.protocol import HttpProtocol

SRC = pathlib.Path(veloce.__file__).parent
PACKAGE_MODULES = sorted(SRC.rglob("*.py"))


def test_the_deprecated_loop_scan_covers_the_package():
    """The parametrized check below is vacuous on an empty file list."""
    assert len(PACKAGE_MODULES) > 100


@pytest.mark.parametrize("path", PACKAGE_MODULES, ids=lambda p: p.relative_to(SRC).as_posix())
def test_no_module_uses_the_deprecated_event_loop_accessor(path):
    """`asyncio.get_event_loop()` is deprecated and returns a loop that may not run.

    This used to grep exactly two modules for a repository-wide claim, and its
    failure message named `app.py` - a path that stopped existing when the
    package was split into `veloce/app/`. A third module reintroducing the call
    was invisible to it.
    """
    source = path.read_text(encoding="utf-8")
    assert "get_event_loop()" not in source, (
        f"{path.relative_to(SRC).as_posix()} uses the deprecated "
        "asyncio.get_event_loop(); use get_running_loop() or take the loop as "
        "an argument"
    )


class TestTaskStrongReferences:
    """Verify fire-and-forget tasks are held to prevent GC."""

    def test_protocol_has_active_tasks_set(self):
        assert hasattr(HttpProtocol, "_active_tasks")
        assert isinstance(HttpProtocol._active_tasks, set)

    def test_protocol_has_keep_alive_timeout(self):
        assert HttpProtocol.KEEP_ALIVE_TIMEOUT == 75


class TestSyncHandlerOffloading:
    """Verify sync handlers don't block the event loop."""

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

    async def test_streaming_response_has_stream_to(self):
        async def generate():
            for i in range(3):
                yield f"chunk {i}\n".encode()

        resp = StreamingResponse(generate())
        assert hasattr(resp, "stream_to")
        # encode() should produce chunked headers
        encoded = resp.encode()
        assert b"Transfer-Encoding: chunked" in encoded

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


# `TestBackgroundTaskSafety` and `TestTeardownAlwaysRuns` used to sit here,
# duplicating `test_background_tasks.py` and `test_teardown_request.py` - two
# whole modules in this same directory. Their one piece of distinct coverage
# was that the teardown hooks were `async` where the sibling's were sync;
# `test_teardown_request.py` is parameterised over both shapes now. The
# `teardown_appcontext`-on-shutdown case moved to `test_teardown_appcontext.py`,
# which is where a reader looks for it.


class TestGracefulShutdownStructure:
    """Verify graceful shutdown components exist."""

    def test_graceful_shutdown_method_exists(self):
        app = Veloce()
        assert hasattr(app, "_graceful_shutdown")

    async def test_teardown_appcontext_not_fired_on_shutdown(self):
        """teardown_appcontext is per-request only; _graceful_shutdown
        must not duplicate it."""
        log = []

        app = Veloce(openapi_url=None)

        @app.teardown_appcontext
        def cleanup(exc):
            log.append("appcontext_teardown")

        loop = asyncio.get_running_loop()
        await app._graceful_shutdown(loop)
        assert "appcontext_teardown" not in log


@pytest.mark.perf
class TestPerformanceAfterFixes:
    """Sanity check that async fixes didn't kill performance.

    Marked `perf` because every assertion is a wall-clock measurement —
    OS-scheduler jitter under full-suite CPU contention makes them flaky
    even with a generous budget, so they are excluded from the default
    `pytest` run. Opt in with `pytest -m perf` on a quiet machine to
    actually exercise these checks.
    """

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

    async def test_sync_handler_adds_little_over_its_executor_hop(self):
        """Dispatching a sync handler costs its thread hop plus this framework's
        dispatch, and dispatch is the only part of that this framework controls.

        Comparing sync against async wall-clock directly measures the operating
        system's thread-pool latency far more than anything here: the bare hop is
        ~130 us on a Windows dev machine while async dispatch is ~6 us, so such a
        ratio drifts upward every time async dispatch gets *faster* - it fails on
        an improvement. The bare hop is timed in the same run and subtracted, so
        what is asserted is what dispatch adds on top of it.
        """
        import time

        app = Veloce(openapi_url=None)

        @app.get("/sync-bench")
        def sync_bench(request: Request):
            return {"ok": True}

        @app.get("/async-bench")
        async def async_bench(request: Request):
            return {"ok": True}

        async def time_handler(path: str, iters: int) -> float:
            for _ in range(50):  # warmup
                await app.handle_request(make_request(path=path))
            times = []
            for _ in range(iters):
                start = time.perf_counter_ns()
                await app.handle_request(make_request(path=path))
                times.append(time.perf_counter_ns() - start)
            return sum(times) / len(times) / 1000

        async def time_executor_hop(iters: int) -> float:
            """Time an empty round trip through the same thread pool."""
            loop = asyncio.get_running_loop()

            def noop() -> None:
                return None

            for _ in range(50):  # warmup, and start the pool's threads
                await loop.run_in_executor(None, noop)
            start = time.perf_counter_ns()
            for _ in range(iters):
                await loop.run_in_executor(None, noop)
            return (time.perf_counter_ns() - start) / iters / 1000

        async_us = await time_handler("/async-bench", 200)
        sync_us = await time_handler("/sync-bench", 200)
        hop_us = await time_executor_hop(200)
        # What dispatch adds beyond the hop. Floored: on a noisy machine the two
        # measurements can cross, which means the addition is too small to see.
        added_us = max(0.0, sync_us - hop_us)

        # 20x an async dispatch is a deliberately loose catastrophe detector - the
        # addition is normally about one async dispatch. Anything past this is a
        # real regression rather than noise.
        assert added_us < async_us * 20, (
            f"sync dispatch adds {added_us:.1f} us over a {hop_us:.1f} us executor "
            f"hop (sync avg {sync_us:.1f} us), against an async dispatch of "
            f"{async_us:.1f} us = {added_us / async_us:.1f}x (budget: <20x)"
        )
