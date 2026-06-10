---
description: >-
  Why Veloce is fast and the measured proof — radix-tree routing, the compiled dependency-graph
  resolver, HandlerPlan precompilation, the zero-cost feature pipeline, JSONResponse.from_bytes,
  the msgspec backend, and the FastAPI-anchored throughput, memory, and cold-start numbers.
tags: [performance, dispatch, benchmarks, msgspec]
---

# Performance

Veloce is fast because the per-request dispatch path does almost no work that
can be moved off it. Routing is a radix tree, the dependency graph compiles to a
straight-line resolver, parameter reflection happens once at registration, and
disabled features cost zero per request.

This page explains each mechanism
against the real source and then states the measured numbers — every figure is
benchmarked, with the methodology and caveats spelled out and cross-linked to
[Benchmarks](../benchmarks.md).

!!! note
    The framework is rarely the bottleneck in a real application — the database
    or the network usually is. These numbers measure dispatch-path headroom, not
    a realistic upper bound on a full app. Spend the headroom; do not expect it
    to mask a slow query.

## Radix-tree routing

The [`Router`](../reference.md#veloce.Router) matches paths against a radix tree,
not a list of compiled regexes. Path parameters are extracted during a single
tree traversal, so lookup cost grows with path depth, not with the number of
registered routes, and a static segment always wins over a parameterised one at
the same position.

```python title="app.py"
from veloce import Veloce

app = Veloce()


@app.get("/items/{item_id}")
async def get_item(item_id: int):
    return {"item_id": item_id}


@app.get("/items/featured")
async def featured():
    return {"items": ["a", "b"]}


if __name__ == "__main__":
    app.run(port=8000)
```

`/items/featured` resolves to `featured` even though `/items/{item_id}` is also
registered: the static child is preferred over the parameter child during
traversal, so declaration order does not matter and you never pay a linear scan.

## HandlerPlan precompilation

Every route is reflected exactly once, at registration, into a `HandlerPlan`
(`src/veloce/_handler_plan.py`). The plan is a list of slots — one per handler
parameter — each tagged with the request source it reads from (path, query,
header, cookie, body model, the request itself, and so on). The expensive
reflection calls, `inspect.signature` and `typing.get_type_hints`, never run on
the request path.

```python title="app.py" hl_lines="13"
from pydantic import BaseModel

from veloce import Request, Veloce

app = Veloce()


class Item(BaseModel):
    name: str
    price: float


@app.post("/items/{item_id}")
async def create_item(item_id: int, item: Item, request: Request):
    return {"item_id": item_id, "name": item.name}


if __name__ == "__main__":
    app.run(port=8000)
```

At registration Veloce records each slot of the handler signature:

| Parameter | Recorded as |
| --- | --- |
| `item_id` | a path int |
| `item` | a body model |
| `request` | the injected request |

Per request it walks that frozen plan instead of re-inspecting the signature.

## The compiled dependency-graph resolver

A linear [`Depends`](../reference.md#veloce.Depends) chain — dependencies with no
parallel-safe batching, no `Security` scopes, no `yield`-teardown, and no body or
async markers — has no concurrency to preserve. For such a graph Veloce generates
a straight-line `async` resolver once at registration
(`src/veloce/_resolver_codegen.py`) that awaits each dependency in order, with no
per-slot dispatch loop, no kind branching, and no slot attribute lookups on the
hot path.

```python title="app.py"
from veloce import Depends, Veloce

app = Veloce()


async def get_db():
    return {"conn": "ok"}


async def get_repo(db: dict = Depends(get_db)):
    return {"repo": db}


@app.get("/users")
async def list_users(repo: dict = Depends(get_repo)):
    return {"repo": repo}


if __name__ == "__main__":
    app.run(port=8000)
```

The `get_db` → `get_repo` → `list_users` chain compiles to one flat async
function. Graphs that genuinely need concurrency, scope mutation, or teardown
keep the general interpreter, which preserves their `asyncio.gather` batching and
stateful semantics — so behaviour is identical and only the flattenable subset is
accelerated.

!!! note
    The compiled resolver is built lazily and cached on the plan. When a plan is
    not compilable (parallel waves, `Security` scopes, `yield` dependencies) the
    resolver falls back to the interpreter transparently; you do not opt in or
    out.

## Zero-cost feature pipeline

App-level features — middleware phases, host/origin gates, ASGI wrappers — are
declared as specs in a feature registry and compiled once into a frozen
`CompiledPipeline` (`src/veloce/_pipeline.py`). Each feature's `enabled()`
predicate runs a single time at compile, enabled specs are bucketed by phase, and
the dispatch core reads one fused slot per phase. A feature you do not use is not
iterated, not predicate-checked, and not branched on per request.

```python title="app.py"
from veloce import Veloce

# CORS, sessions, rate limiting, and similar features are off unless configured.
# A bare app pays for none of them on the request path.
app = Veloce()


@app.get("/")
async def index():
    return {"ok": True}


if __name__ == "__main__":
    app.run(port=8000)
```

The pipeline recompiles only when the app's generation counter advances; in
production the counter freezes once setup latches, so the pipeline compiles
exactly once and the hot path never touches the registry.

## Zero-recopy JSON responses

[`JSONResponse.from_bytes`](../reference.md#veloce.JSONResponse) builds a response
from JSON bytes that are already encoded, skipping the orjson re-encode that the
normal constructor performs. Use it when the caller has produced the JSON body
itself — for example via a cache, a custom `orjson` option set, or a
`JSONProvider.dumps`.

```python title="app.py"
import orjson

from veloce import JSONResponse, Veloce

app = Veloce()

# Encoded once at startup; served verbatim with no per-request re-encode.
_CACHED = orjson.dumps({"status": "ok", "region": "eu-west-1"})


@app.get("/health")
async def health():
    return JSONResponse.from_bytes(_CACHED)


if __name__ == "__main__":
    app.run(port=8000)
```

The body is sent verbatim with `Content-Type` taken from the response class's
`default_media_type`, so a `JSONResponse` subclass keeps its declared media type
without overriding the method. The default JSON path itself encodes through
orjson, which keeps the common return-a-dict case fast without any special call.

## The msgspec backend

On validation- and serialisation-heavy endpoints you can opt a single endpoint
into the [msgspec](https://jcristharif.com/msgspec/) backend by typing a body
parameter as a `msgspec.Struct` instead of a Pydantic `BaseModel`. The choice is
per endpoint, by type — there is no global switch — so Pydantic and msgspec
endpoints coexist in one app.

```python title="app.py"
import msgspec

from veloce import Veloce

app = Veloce()


class User(msgspec.Struct):
    name: str
    age: int


@app.post("/users")
async def create_user(user: User):
    return {"name": user.name, "age": user.age}


if __name__ == "__main__":
    app.run(port=8000)
```

Measured against Pydantic, the msgspec backend is roughly **5.9x** faster on
isolated serialisation and roughly **1.27x** faster end-to-end through the full
request path. See the [msgspec backend](../guide/msgspec.md) guide for the
validation-error shape difference and when the trade-off is worth it.

## Measured results

Every number below is a measurement, not a projection. The methodology and the
caveats that bound it are in [Benchmarks](../benchmarks.md); read that page
before quoting any figure.

### Throughput against FastAPI and Flask

Across 35 scenarios, anchored to FastAPI on the same machine, Veloce's geometric
mean throughput is about **2.0x** FastAPI — `1.92x` at 16 connections, `2.03x` at
64, and `2.06x` at 256. Veloce wins **every one** of the 35 scenarios, with zero
regressions. Against Flask the geometric mean is about **5.3x**.

| Metric | Measured |
| --- | --- |
| Throughput vs FastAPI (geomean, 35 scenarios) | `~2.0x` |
| At 16 / 64 / 256 connections | `1.92x` / `2.03x` / `2.06x` |
| Scenarios won vs FastAPI | `35 / 35` (0 regressions) |
| Throughput vs Flask (geomean) | `~5.3x` |

!!! warning "Use the geomean, not a single scenario"
    Per-scenario run-to-run variance is about **±5%**, so a cherry-picked
    scenario can over- or under-state the gap. The **FastAPI-anchored
    35-scenario geometric mean** is the figure to quote; a lone scenario is not.

### Eleven-framework arena

In an 11-framework run ranked by geometric-mean requests per second, Veloce
places **2nd of 11**. It trails only aiohttp — a bare async HTTP server with no
routing, validation, or dependency injection — and ranks ahead of Cython-backed
BlackSheep, Starlette, Litestar, Sanic, FastAPI, Tornado, Quart, Flask, and
Django.

!!! note
    aiohttp does no routing, validation, or dependency injection, so it is not a
    like-for-like comparison. Among full request frameworks that validate input
    and resolve dependencies, Veloce is the fastest in this run.

### Memory and cold start

Under sustained real-server load Veloce holds about **154 MB** RSS against
FastAPI's **171 MB** — roughly **10% lower**. Per-request retention is about
**0 bytes**: RSS plateaus rather than climbing, so there is no leak. Under
`tracemalloc`, FastAPI retained about **10x** more per request than Veloce.

Cold start — importing the package and building a minimal app — is about
**147 ms** for Veloce versus **244 ms** for FastAPI, roughly **1.65x** faster.

| Metric | Veloce | FastAPI |
| --- | --- | --- |
| RSS under sustained load | `~154 MB` | `~171 MB` |
| Per-request retention | `~0 bytes` (plateaus) | `~10x` Veloce (tracemalloc) |
| Cold start (import + minimal app) | `~147 ms` | `~244 ms` |

!!! warning "Single-box methodology"
    The benchmarks run the load generator and the servers on one host, on
    disjoint pinned cores. Sharing the box makes **absolute** RPS a few percent
    low, so treat the absolute throughput as a floor. The **relative** numbers —
    every ratio above — are valid because both sides run under identical
    conditions in the same process invocation.

## Next steps

- [Benchmarks](../benchmarks.md) — the full methodology, the bench suite layout, and how to reproduce these numbers locally.
- [msgspec backend](../guide/msgspec.md) — opt an endpoint into msgspec validation and serialisation.
- [Migrating from FastAPI](migrating-from-fastapi.md) — the divergence map and the Veloce-only wins.
- [Native server deep dive](native-server.md) — the `HttpProtocol` request loop and its hardening knobs.
- Full signatures are in the [API reference](../reference.md).
