---
description: >-
  Measured throughput against FastAPI, Flask and Tornado, the method used to
  produce the numbers, and what they do and do not tell you.
tags: [performance, benchmarks, methodology]
---

# Benchmarks

[Performance](performance.md) explains the mechanisms. This page reports what
they measure out to, and — more usefully — how the measurement was taken, so
you can judge the numbers rather than take them.

!!! warning "Read the method before the table"
    A benchmark is an argument, not a fact. Every figure here comes from one
    machine, one workload shape and one afternoon. The **ratios** travel
    further than the absolute numbers, and neither predicts your application:
    the framework is rarely the bottleneck in real code. See
    [What this does not measure](#what-this-does-not-measure).

## Method

| | |
|---|---|
| Machine | Linux, otherwise idle |
| Python | 3.12.13, CPython |
| Load generator | `wrk`, 64 connections, on 2 dedicated cores |
| Server | one worker, on 2 dedicated cores |
| Rounds | two per framework, **alternated** rather than run back to back |
| Reported | per-scenario median across rounds |

Three details do most of the work in making these trustworthy:

**Alternation.** Frameworks are not measured one after another. A machine that
drifts mid-run would otherwise hand the whole drift to whichever framework ran
second. Rounds interleave, and the per-scenario median is taken across them.

**Core pinning.** The server and the load generator never share a core. An
unpinned run mostly measures which process won the scheduler.

**Identical applications.** Each framework serves the same route table, the
same response bodies and the same validation work. Where a framework cannot
express a scenario, it is recorded as absent rather than substituted with
something easier.

## Throughput

Requests per second at 64 connections, single worker. Higher is better.

| Scenario | Veloce | FastAPI | Flask | Tornado |
|---|---:|---:|---:|---:|
| plaintext | **35,617** | 21,909 | 4,065 | 11,835 |
| json | **34,112** | 19,776 | 3,925 | 8,414 |
| path_param | **30,221** | 15,729 | 3,793 | 8,213 |
| query | **27,305** | 13,607 | 3,739 | 7,720 |
| dependency_injection | **30,817** | 15,079 | — | — |
| pydantic_body | **27,342** | 13,934 | — | — |
| nested_body | **25,375** | 12,845 | — | — |
| middleware_cors | **27,654** | 17,525 | 3,726 | — |
| session (cookie) | **19,753** | 13,886 | 2,831 | — |
| file_response | **17,382** | 2,256 | 2,930 | 7,455 |
| streaming_response | **27,154** | 6,426 | 2,957 | 6,011 |

**Geometric mean against the best other framework in each scenario: 1.97×**
over the 14 scenarios measured. A wider 35-scenario run of the same suite
gives 2.04×.

A dash means the framework has no equivalent in that scenario — Flask has no
dependency-injection or Pydantic-body path to compare, and Tornado has no
session middleware. Absent is recorded as absent; nothing is substituted.

### Where the gap is largest

The wide margins are not in dispatch, they are where Veloce does less work per
byte: `streaming_response` (4.2× the best other) and `file_response` (2.3×)
avoid a copy that the comparison frameworks make. The narrowest margins —
plaintext at 1.6× — are the scenarios where almost nothing happens per request
and every framework is close to its floor.

## MCP

Veloce's MCP server is measured against [FastMCP](https://github.com/jlowin/fastmcp)
3.4.7 on the same machine. These are per-operation costs with one request in
flight, so the primary figure is **server CPU per operation**, not throughput.

| Operation | Veloce | FastMCP | CPU delta |
|---|---:|---:|---:|
| `tools/call`, trivial | 384 µs | 1,450 µs | **−73%** |
| `tools/call`, structured output | 440 µs | 2,403 µs | **−82%** |
| `tools/list`, 320 tools | 650 µs | 10,042 µs | **−94%** |
| Resident memory | **52 MB** | 88 MB | −41% |

Two honesty notes on that comparison. FastMCP writes roughly 60% more response
bytes on the simplest operations, because it duplicates a dict result into
`structuredContent`; part of the trivial-call delta is wire volume rather than
dispatch. And FastMCP validates arguments more strictly than Veloce does — it
publishes `additionalProperties: false` — so part of the delta is Veloce doing
less work, not doing the same work faster. The `structured_output` row is the
fairest of the three, because response sizes there are within 5% of each other.

## What this does not measure

- **Your application.** These exercise dispatch-path headroom. A request that
  waits on a database spends its time there, and no framework will change that.
- **More than one worker.** Every figure is a single worker.
  Multi-process scaling is a property of the deployment, not the framework —
  see [Server Workers](../deployment/workers.md).
- **Latency under saturation.** Throughput at a fixed connection count says
  little about tail latency when a service is overloaded.
- **The native server.** Every number here is Veloce under an ASGI server,
  which is the documented production path. The built-in `HttpProtocol` — used
  by `app.run()` and the gunicorn `VeloceWorker` — costs roughly 24% more CPU
  per request, since its HTTP parser is pure Python rather than C. See
  [The native server](native-server.md).
- **Cold start, memory under load, or long-run behaviour.** Different
  questions, measured differently.

## Reproducing this

The suite is not shipped in the package — it is a development tool, and its
results depend on the host far more than on the code. What matters if you want
to check the claims is the method above: identical applications, alternated
rounds, pinned cores, and the per-scenario median reported rather than the best
round.

If you benchmark Veloce yourself and get a materially different answer, that is
worth an issue. Please include the host, the Python version, the worker count
and the load generator's own CPU usage — a load generator that saturates before
the server does is the most common way a web-framework benchmark goes wrong,
and it produces numbers that look like a server ceiling.

## Next steps

- [Performance](performance.md) — the mechanisms behind these numbers
- [Veloce vs Starlette](veloce-vs-starlette.md) — a design comparison
- [Server Workers](../deployment/workers.md) — scaling beyond one process
