---
description: Veloce performance benchmarks — request throughput, latency, memory usage, and how to run the benchmark suite locally.
tags: [benchmarks, performance]
---

# Benchmarks

Veloce ships a benchmark suite under `bench/` that measures the
framework against itself across releases, and — for a small set of
workloads — against FastAPI and Flask on the same machine. This page
describes the methodology so you can reproduce the numbers locally
before quoting them.

Numbers are workload-specific. A microbenchmark that exercises the
dispatch hot path is not a substitute for measuring your own
application under realistic load.

## Methodology

The bench suite isolates the framework cost from the network and
serialisation cost where possible.

- **In-process driver.** The framework-internal benches (`bench_*.py`, `dispatch_bench.py`) call the application's ASGI surface directly inside a single event loop. There is no socket, no separate worker process, and no `httpx` round trip. This isolates the dispatch and middleware cost from kernel I/O and HTTP parsing.
- **Comparative driver.** The `bench/comparative/` harness runs each app under the same single-worker uvicorn in randomised order, discards a leading cold-cache round, and prints a median over multiple runs. This is the harness used for cross-framework comparisons.
- **Warmup.** Every bench warms the path for a few hundred to a thousand iterations before timing. This forces JIT-equivalent state (function specialisation, attribute caches, opcode caching) to settle so that the first measured iteration is not paying for one-time setup.
- **GC discipline.** Hot loops disable `gc.collect` for the timed window and re-enable afterwards so a stop-the-world pause inside the window does not skew the per-op number.
- **Workloads.** The comparative bench currently ships two workloads: `json-hello` (`GET /` returning a small JSON object) and `path-param` (`GET /items/{id}` returning the parsed id). Real applications spend most of their time in user code, ORM queries, and template rendering — these workloads measure the framework floor, not a realistic upper bound.

## Run the benchmarks

Install the package in editable mode with dev dependencies:

```bash
pip install -e ".[dev]"
```

Run the in-process dispatch bench:

```bash
python bench/dispatch_bench.py
```

Run the cross-framework comparison (requires FastAPI and Flask
installed in the same environment):

```bash
pip install fastapi flask uvicorn asgiref
python -m bench.comparative.bench json-hello
python -m bench.comparative.bench path-param
```

## What the comparison measures

Each bench prints one or more of:

- **Requests per second.** Iterations divided by elapsed time over a fixed iteration count.
- **Microseconds or nanoseconds per request.** Per-op latency, useful for comparing two implementations of the same path on the same machine.
- **Memory footprint.** A subset of benches (`mem_dispatch_bench.py`) wrap the workload in `tracemalloc` to record peak allocation per request.

The comparative bench reports the median over several timed runs to
absorb runner-to-runner variance.

## Bench scripts in this repo

The `bench/` directory holds the individual scripts. Each is runnable
standalone.

| Script | What it measures |
|--------|------------------|
| `bench/dispatch_bench.py` | End-to-end requests/second for static, path-param, and POST-body routes through the in-memory `TestClient`. Supports `--min-rps` for CI regression gating. |
| `bench/hot_dispatch_bench.py` | Tight loop over the resolved hot path, no setup per iteration. |
| `bench/mem_dispatch_bench.py` | Peak allocation per dispatched request via `tracemalloc`. |
| `bench/profile_dispatch.py` | `cProfile` driver for one dispatch path — use to attribute time to specific frames. |
| `bench/profile_hot.py` | `cProfile` driver for the resolved hot path. |
| `bench/compare_inloop.py` | Veloce vs FastAPI inside a single event loop — useful for diffing CPU cost without crossing the loop boundary. |
| `bench/comparative/bench.py` | Cross-framework comparative harness (Veloce vs FastAPI vs Flask) under uvicorn, randomised order, median over runs. |

## Interpreting results

A few caveats worth keeping in mind:

- **Microbenchmarks are not production load.** They exercise a single code path with no template render, no database call, no external HTTP, no TLS termination. Real services spend their time elsewhere. Use the dispatch numbers to compare framework cost across versions, not to predict throughput for your application.
- **Single-worker uvicorn is not the only deployment shape.** WSGI servers under multiple workers exhibit different scaling characteristics. The comparative bench measures one specific deployment model; read the per-row numbers in that context.
- **Variance is real.** Background processes, CPU thermal throttling, and memory pressure all move the numbers. Run a bench multiple times and prefer the median over the best.
- **CPython version matters.** Each minor release changes attribute caches, dispatch costs, and asyncio internals. Note the interpreter version next to any number you publish.
- **An audit on paper is not a profile.** Static "this looks slow" claims often do not show up in a profiler against a realistic workload. Profile before optimising and re-bench after.

## Source

The full benchmark suite is in the
[`bench/` directory](https://github.com/Lokesh-Tallapaneni/veloce/tree/main/bench)
on GitHub. Open an issue or pull request to add a new workload or
correct an existing one.

## See also

- [Comparison with other frameworks](comparison.md)
- [Deployment guide](guide/deployment.md)
