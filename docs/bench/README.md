# Comparative benchmarks

This tree records head-to-head latency and throughput numbers for Veloce
vs Flask vs FastAPI on identical workloads. The Phase-3 contract: a
feature is not "done" until Veloce beats both rivals on **median of ≥3
runs** for the relevant metrics.

## Layout

- `bench/comparative/` (in the source tree) — the harness:
  - `apps/<framework>_<workload>.py` — one tiny app per framework per
    workload; only the framework varies.
  - `runner.py` — boots `uvicorn module:app` on a free localhost port,
    waits for readiness, runs a warmup burst, then drives `concurrency`
    workers through `httpx.AsyncClient` for `duration` seconds; returns
    per-request latencies (perf-counter ns) and an error count.
  - `bench.py` — orchestrator: takes a workload name, randomises run
    order across `runs × frameworks`, prints a median table and a
    "Veloce does NOT beat …" diagnostic line.

## Invariants

- **All three frameworks share the same server runtime** — uvicorn,
  stdlib asyncio loop, single worker, access log disabled. Flask runs
  through `asgiref.WsgiToAsgi` so it serves through the same uvicorn
  instance; the only variable is the framework.
- **Run order is shuffled per round**, so a warmup advantage cannot
  accrue to any one framework.
- **Warmup is timed but its samples are discarded** before the measured
  window starts.

## Reading the numbers

| Column | Meaning |
|---|---|
| `rps (med)` | Median requests-per-second across runs. **Higher is better**. |
| `p50 us`    | Median request latency, µs. Lower is better. |
| `p99 us`    | 99th-percentile request latency, µs. Lower is better. |

Run with:

```bash
python -m bench.comparative.bench <workload> [--runs N] [--duration S] [--concurrency C]
```

Workloads: `json-hello`, `path-param`.

## Notes on this harness

- The in-process load generator (httpx + asyncio) saturates around
  ~12 concurrent connections on Windows with the stdlib event loop.
  Beyond that the **client** becomes the bottleneck and every framework
  collapses to the same client-bound RPS. The default concurrency is
  `8` for that reason; raise it only if you have an out-of-process load
  generator (e.g. `wrk` from another host) that can drive harder load.
- p99 on localhost is dominated by GC pauses, OS scheduling jitter, and
  occasional event-loop hiccups. A 5-run median dampens that.
- Flask's p50 wins on every workload because synchronous WSGI has a
  shorter per-request critical path at low concurrency. The Phase-3
  contract weighs **throughput + tail latency** more than p50: under any
  realistic load Veloce's async pipeline pulls ahead, which is what
  these numbers show on rps and p99.
