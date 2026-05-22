"""Drive load against a single ASGI app — measure latency and throughput.

Boots `uvicorn module:app` in a subprocess on a free localhost port,
waits for it to answer, sends a warmup burst, then runs a timed window
of concurrent requests through `httpx.AsyncClient`. Returns per-request
latencies (perf-counter ns) so the caller can compute p50/p99/rps and
summarise across runs.

See `docs/bench/README.md` "Notes on this harness" for concurrency and
deployment caveats.
"""

from __future__ import annotations

import asyncio
import math
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
        # Nearest-rank p99: position ceil(0.99 * n), 1-indexed. The plain
        # `int(0.99 * n) - 1` truncation was one slot low whenever
        # `0.99 * n` was non-integer, slightly under-reporting p99.
        idx = min(len(s) - 1, max(0, math.ceil(0.99 * len(s)) - 1))
        return s[idx] / 1000


class ReadinessTimeout(RuntimeError):
    """Server did not answer within the readiness deadline.

    Raised distinctly from a body-mismatch (which is also a `RuntimeError`
    but a programming error, not a flaky-port symptom). `measure` only
    retries on this one, so a genuine bad-body response surfaces
    immediately instead of eating another readiness window.
    """


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_ready(
    client: httpx.AsyncClient,
    url: str,
    expected_substring: bytes | None = None,
    deadline_s: float = 10.0,
) -> None:
    """Poll until the server answers with a non-error response, or time out.

    A non-error response is `200 <= status < 400`; a `405`/`5xx` reply
    means the framework is not actually serving this workload yet.
    If `expected_substring` is given, the response body must contain
    it — catches a regression that returns 200 with empty / wrong
    payload, without tripping on the three frameworks' subtly different
    JSON whitespace and key ordering.
    """
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            resp = await client.get(url, timeout=0.5)
            if 200 <= resp.status_code < 400:
                if expected_substring is not None and expected_substring not in resp.content:
                    raise RuntimeError(
                        f"server at {url} returned body that does not contain "
                        f"{expected_substring!r} (got {resp.content!r})"
                    )
                return
        except (httpx.HTTPError, OSError):
            pass
        await asyncio.sleep(0.05)
    raise ReadinessTimeout(f"server at {url} did not become ready within {deadline_s}s")


async def _drive_load(
    client: httpx.AsyncClient,
    url: str,
    duration_s: float,
    concurrency: int,
) -> tuple[list[int], int, float]:
    """Hit `url` flat-out for `duration_s` seconds with `concurrency` workers.

    Returns `(latencies_ns, errors, elapsed_s)`. The caller owns the
    `httpx.AsyncClient` so connection-pool spin-up is paid out-of-band
    (during warmup) and does not inflate the measured window.
    """
    latencies: list[int] = []
    errors = 0
    start = time.perf_counter()
    stop_at = start + duration_s

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
    return latencies, errors, time.perf_counter() - start


async def _measure_once(
    framework: str,
    module: str,
    duration_s: float,
    warmup_s: float,
    concurrency: int,
    path: str,
    expected_substring: bytes | None,
) -> RunResult:
    """One full attempt: bind a port, boot uvicorn, drive load, tear down."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}{path}"
    proc: subprocess.Popen[bytes] | None = None
    try:
        # `Popen` returns immediately — it does not block the event loop;
        # the long-lived process is reaped via `terminate`/`wait` in the
        # `finally` block. ASYNC220 fires on the *type* of call, not the
        # actual blocking behaviour here. The `Popen` lives inside `try`
        # so that a Ctrl-C between Popen and the try-body still tears
        # down the child instead of orphaning it.
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
        limits = httpx.Limits(
            max_connections=concurrency,
            max_keepalive_connections=concurrency,
        )
        async with httpx.AsyncClient(timeout=5.0, limits=limits) as client:
            await _wait_ready(client, url, expected_substring=expected_substring)
            # Warmup discards its samples; it also spins up the
            # connection pool so the measured window does not pay for it.
            await _drive_load(client, url, warmup_s, concurrency)
            latencies, errors, elapsed = await _drive_load(client, url, duration_s, concurrency)
    finally:
        if proc is not None:
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


async def measure(
    framework: str,
    module: str,
    duration_s: float = 4.0,
    warmup_s: float = 1.0,
    concurrency: int = 8,
    path: str = "/",
    expected_substring: bytes | None = None,
    retries: int = 1,
) -> RunResult:
    """Boot `module:app` under uvicorn, drive load, return latencies.

    The default `concurrency=8` matches the discriminating regime
    documented in `docs/bench/README.md` — the in-process httpx +
    asyncio load generator saturates above ~12 on Windows / stdlib loop.
    Raise it only if you know your client can drive harder load.

    `retries` covers the `_free_port` TOCTOU window: between freeing the
    port and uvicorn's bind, another process can grab it. On a readiness
    failure we retry once with a fresh port.
    """
    last_exc: ReadinessTimeout | None = None
    for _ in range(retries + 1):
        try:
            return await _measure_once(
                framework=framework,
                module=module,
                duration_s=duration_s,
                warmup_s=warmup_s,
                concurrency=concurrency,
                path=path,
                expected_substring=expected_substring,
            )
        except ReadinessTimeout as exc:
            # Only the port-race / cold-start timeout is retried. A
            # body-mismatch `RuntimeError` from `_wait_ready` is a real
            # framework bug; let it surface immediately instead of
            # burning another ~10 s readiness window on a retry.
            last_exc = exc
            continue
    assert last_exc is not None
    raise last_exc
