---
description: >-
  Why Veloce is fast — radix-tree routing, the compiled dependency-graph resolver, HandlerPlan
  precompilation, the zero-cost feature pipeline, JSONResponse.from_bytes, and the msgspec backend.
tags: [performance, dispatch, msgspec]
---

# Performance

Veloce is fast because the per-request dispatch path does almost no work that
can be moved off it. Routing is a radix tree, the dependency graph compiles to a
straight-line resolver, parameter reflection happens once at registration, and
disabled features cost zero per request.

This page explains each mechanism against the real source.

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

On serialisation-heavy endpoints the msgspec backend can lower encode/decode
cost relative to Pydantic; measure it on your own workload before adopting it.
See the [msgspec backend](../guide/msgspec.md) guide for the validation-error
shape difference and when the trade-off is worth it.

## Next steps

- [msgspec backend](../guide/msgspec.md) — opt an endpoint into msgspec validation and serialisation.
- [Migrating from FastAPI](migrating-from-fastapi.md) — the divergence map and the Veloce-only wins.
- [Native server deep dive](native-server.md) — the `HttpProtocol` request loop and its hardening knobs.
- Full signatures are in the [API reference](../reference.md).
