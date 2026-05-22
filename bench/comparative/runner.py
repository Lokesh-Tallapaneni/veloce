"""Drive load against a single ASGI app — measure latency and throughput.

Boots `uvicorn module:app` in a subprocess on a free localhost port,
waits for it to answer, sends a warmup burst, then runs a timed window
of concurrent requests through `httpx.AsyncClient`. Returns per-request
latencies (perf-counter ns) so the caller can compute p50/p99/rps and
summarise across runs.
"""

from __future__ import annotations

import asyncio
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx


@dataclass
class RunResult:
    framework: str
    latencies_ns: list[int]
    elapsed_s: float
    errors: int

    @property
    def rps(self) -> float:
        return len(self.latencies_ns) / self.elapsed_s if self.elapsed_s else 0.0

    @property
    def p50_us(self) -> float:
        return statistics.median(self.latencies_ns) / 1000

    @property
    def p99_us(self) -> float:
        if not self.latencies_ns:
            return 0.0
        s = sorted(self.latencies_ns)
        idx = max(0, int(len(s) * 0.99) - 1)
        return s[idx] / 1000


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_ready(url: str, deadline_s: float = 10.0) -> None:
    """Poll until the server answers with any HTTP response, or time out."""
    end = time.monotonic() + deadline_s
    async with httpx.AsyncClient() as client:
        while time.monotonic() < end:
            try:
                resp = await client.get(url, timeout=0.5)
                if resp.status_code < 500:
                    return
            except (httpx.HTTPError, OSError):
                pass
            await asyncio.sleep(0.05)
    raise RuntimeError(f"server at {url} did not become ready within {deadline_s}s")


async def _drive_load(
    url: str,
    duration_s: float,
    concurrency: int,
) -> tuple[list[int], int]:
    """Hit `url` flat-out for `duration_s` seconds with `concurrency` workers."""
    latencies: list[int] = []
    errors = 0
    stop_at = time.perf_counter() + duration_s

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=5.0, limits=limits) as client:

        async def worker() -> None:
            nonlocal errors
            while time.perf_counter() < stop_at:
                t0 = time.perf_counter_ns()
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        errors += 1
                        continue
                except httpx.HTTPError:
                    errors += 1
                    continue
                latencies.append(time.perf_counter_ns() - t0)

        await asyncio.gather(*(worker() for _ in range(concurrency)))

    return latencies, errors


async def measure(
    framework: str,
    module: str,
    duration_s: float = 4.0,
    warmup_s: float = 1.0,
    concurrency: int = 32,
    path: str = "/",
) -> RunResult:
    """Boot `module:app` under uvicorn, drive load, return latencies."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}{path}"
    # `Popen` returns immediately — it does not block the event loop;
    # the long-lived process is reaped via `terminate`/`wait` in the
    # `finally` block. ASYNC220 fires on the *type* of call, not the
    # actual blocking behaviour here.
    proc = subprocess.Popen(  # noqa: S603,ASYNC220
        [
            sys.executable,
            "-m",
            "uvicorn",
            f"{module}:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "critical",
            "--loop",
            "asyncio",
            "--no-access-log",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        await _wait_ready(url)
        # Warmup — JIT-ish caches, fill keep-alive pools, settle the loop.
        await _drive_load(url, warmup_s, concurrency)
        t0 = time.perf_counter()
        latencies, errors = await _drive_load(url, duration_s, concurrency)
        elapsed = time.perf_counter() - t0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    return RunResult(
        framework=framework,
        latencies_ns=latencies,
        elapsed_s=elapsed,
        errors=errors,
    )
