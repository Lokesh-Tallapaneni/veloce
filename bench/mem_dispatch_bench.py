"""Per-request allocation + peak memory bench for the dispatch hot path.

Wraps `bench.hot_dispatch_bench`'s in-loop driver in `tracemalloc` so we can
report:
  * total bytes allocated for N requests (delta of allocated-since-snapshot
    counters, isolates this run from import-time noise)
  * peak heap during the run (returned to caller)
  * resident set size before/after (psutil, if available — Windows-friendly)

Goal: give the perf audit an honest memory-axis number to put next to the
existing CPU / timing numbers, since the original audit only covered the
latter two.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import tracemalloc

from bench.hot_dispatch_bench import build_app, make_scope

try:
    import psutil  # type: ignore[import-untyped]
except ImportError:
    psutil = None  # type: ignore[assignment]


def _rss_bytes() -> int | None:
    if psutil is None:
        return None
    return psutil.Process().memory_info().rss


async def _one(app, scope, body):
    sent: list = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    await app(scope, receive, send)


async def measure(label: str, app, scope, body: bytes, iters: int, warmup: int) -> dict:
    # Warmup — populate caches, JIT-equivalent paths, fill freelists.
    for _ in range(warmup):
        await _one(app, scope, body)
    gc.collect()

    # ── Run 1: GC enabled — measure NET retained memory and live peak.
    #   This number is the "leak" check: anything > a few bytes per
    #   request means the dispatch is holding onto state across requests.
    rss_before = _rss_bytes()
    tracemalloc.start()
    base_alloc, _base_peak = tracemalloc.get_traced_memory()
    for _ in range(iters):
        await _one(app, scope, body)
    end_alloc, peak_with_gc = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = _rss_bytes()
    net_retained = end_alloc - base_alloc

    # ── Run 2: per-request peak — reset the tracemalloc peak between
    #   iterations and average. CPython's refcount frees most
    #   transients immediately, so the "churn" is invisible to a
    #   start/stop pair; the meaningful number for concurrent-request
    #   memory pressure is the high-water mark DURING a single
    #   dispatch. We measure that by resetting peak each iteration.
    gc.collect()
    per_req_peaks: list[int] = []
    tracemalloc.start()
    peak_iters = min(iters, 2_000)
    for _ in range(peak_iters):
        tracemalloc.reset_peak()
        await _one(app, scope, body)
        _, p = tracemalloc.get_traced_memory()
        per_req_peaks.append(p)
    tracemalloc.stop()
    per_req_peaks.sort()
    peak_p50 = per_req_peaks[len(per_req_peaks) // 2]
    peak_p99 = per_req_peaks[min(len(per_req_peaks) - 1, int(len(per_req_peaks) * 0.99))]

    return {
        "label": label,
        "iters": iters,
        "retained_bytes_total": net_retained,
        "retained_bytes_per_req": net_retained / iters,
        "per_req_peak_p50": peak_p50,
        "per_req_peak_p99": peak_p99,
        "rss_before": rss_before,
        "rss_after": rss_after,
        "rss_delta": (rss_after - rss_before) if rss_before and rss_after else None,
    }


async def amain(iters: int, warmup: int) -> None:
    app = build_app()
    await app._run_lifecycle("startup")

    cases = [
        ("static GET     ", make_scope("GET", "/"), b""),
        ("path-param GET ", make_scope("GET", "/items/42"), b""),
        ("POST 64 body   ", make_scope("POST", "/echo"), b"x" * 64),
    ]

    print("veloce dispatch memory bench")
    print(f"  iterations: {iters}, warmup: {warmup}")
    for label, scope, body in cases:
        r = await measure(label, app, scope, body, iters, warmup)
        print(
            f"  {r['label']} "
            f"per-req peak p50={r['per_req_peak_p50']:>6,d} B  "
            f"p99={r['per_req_peak_p99']:>6,d} B  "
            f"retained/req={r['retained_bytes_per_req']:>+5.2f} B  "
            + (
                f"rss d={r['rss_delta'] / 1024:>+5.0f} KB"
                if r["rss_delta"] is not None
                else "rss n/a"
            )
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=20_000)
    ap.add_argument("--warmup", type=int, default=2_000)
    args = ap.parse_args()
    asyncio.run(amain(args.iters, args.warmup))


if __name__ == "__main__":
    main()
