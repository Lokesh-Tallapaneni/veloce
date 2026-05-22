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
- The orchestrator runs an extra **cold-cache round** before the
  measured rounds and discards it. The OS file cache, Python `__pycache__`,
  and kernel TCP cache are warm by the time the timed window starts, so
  the first framework in the schedule doesn't pay a one-time penalty.
  Disable with `--no-cold-round` if you want to inspect that overhead.
- Run order is shuffled with a random seed printed at the top of every
  output. Pin it with `--seed N` for byte-exact reproducibility.

## Caveats — read before quoting any number out of context

These benchmarks compare **"same server runtime, only the framework
varies"** — Veloce, FastAPI, and Flask all running under one
single-worker uvicorn with the stdlib asyncio loop. The numbers
**are not** "Veloce vs Flask in production."

- **Flask is run via `asgiref.WsgiToAsgi`** — its handlers execute in a
  thread pool that `WsgiToAsgi` schedules from the asyncio loop. This
  is not Flask's typical deployment. A Flask app served by
  `gunicorn --workers N --worker-class sync` (or by waitress, mod_wsgi,
  …) has a very different throughput profile and would not be directly
  comparable to these numbers.
- **Single uvicorn worker for all three frameworks** — production
  deployments use multiple workers; the relative ordering may change
  under multi-worker setups.
- **`--loop asyncio`** is forced because uvloop is excluded on Windows;
  on Linux, uvloop would lift all three frameworks roughly equally.
- **Flask wins p50 and p99 on every workload** because synchronous WSGI
  through `WsgiToAsgi` has the shortest per-request critical path at
  low concurrency and avoids event-loop scheduling gaps that show up at
  the 99th percentile. Veloce wins **rps** decisively (~57 % more than
  Flask under this shim) and wins **rps + p50 + p99** vs FastAPI. The
  picture would change under multi-worker Flask (gunicorn-sync) and
  under higher concurrency — see the "Caveats" section above.

Quote the recorded numbers as relative measurements **within this
harness**, not as unconditional claims.
