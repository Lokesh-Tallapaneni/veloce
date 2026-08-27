"""Wall-clock dispatch sanity checks (opt-in via `pytest -m perf`)."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce


@pytest.mark.perf
class TestPerformance:
    """Wall-clock dispatch checks — flaky under full-suite CPU contention,
    so the class is marked `perf` and excluded from the default `pytest`
    run. Opt in with `pytest -m perf` on a quiet machine.
    """

    async def test_simple_route_under_50us(self):
        """Sanity check: simple route should complete in under 50 microseconds."""
        import time

        app = Veloce(openapi_url=None)

        @app.get("/bench")
        async def bench(request: Request):
            return {"ok": True}

        # Warmup
        for _ in range(100):
            await app.handle_request(make_request(path="/bench"))

        # Measure
        times = []
        for _ in range(1000):
            start = time.perf_counter_ns()
            await app.handle_request(make_request(path="/bench"))
            times.append(time.perf_counter_ns() - start)

        avg_us = sum(times) / len(times) / 1000
        # The name, the docstring and this number used to disagree: both said
        # 50us and the assertion allowed 100. Measured here, dispatch averages
        # ~9us, so the documented 50us budget holds with 5x headroom - and the
        # module is `perf`-marked and deselected by default precisely so the
        # number can mean something on a quiet machine.
        assert avg_us < 50, f"Average request time {avg_us:.1f}us exceeds the 50us budget"
