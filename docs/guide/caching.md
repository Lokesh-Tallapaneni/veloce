---
description: Cache a handler or async function's result in Veloce with the cached decorator, an in-memory or Redis backend, and custom cache keys.
---

# Caching

Veloce caches **results**, not whole responses: the `cached` decorator memoises an
async function's JSON-serialisable return in a `Cache` backend, keyed by the call
arguments. It is fully opt-in and self-contained — the request pipeline imports
none of it, so a handler you do not decorate pays nothing.

```python
from veloce import InMemoryCache, Veloce, cached

app = Veloce()
cache = InMemoryCache()


@app.get("/reports/{report_id}")
@cached(cache, ttl=60)
async def report(report_id: int) -> dict:
    return build_expensive_report(report_id)  # runs at most once per id per 60s
```

The first call to `/reports/7` runs the handler and stores the result; calls
within the next 60 seconds return the cached value without re-running it. Put
`@cached` **below** the route decorator so the route registers the wrapped
handler.

## Cache keys

By default the key is the function's qualified name plus a digest of its
arguments. Arguments that are not JSON-serialisable — an injected `Request`, a
`Depends` result — are ignored, so a handler is keyed by its scalar inputs:

```python
@app.get("/items/{item_id}")
@cached(cache, ttl=30)
async def get_item(item_id: int, request: Request) -> dict:
    # Keyed by item_id only; the request object does not affect the key.
    return {"item_id": item_id}
```

Pass `key=` a callable taking the same arguments for full control — and use it
when two same-named functions (for example closures from the same factory) share
one cache, since the default key uses the function's qualified name:

```python
@cached(cache, ttl=300, key=lambda user_id: f"user:{user_id}")
async def load_user(user_id: int) -> dict:
    return await db_lookup(user_id)
```

## What can be cached

- The function must be `async`.
- The result must be **JSON-serialisable** (a Pydantic model is dumped in JSON
  mode); a non-serialisable result raises `TypeError`.
- A cache **hit returns the JSON-decoded value** (a plain `dict`/`list`/scalar),
  not the original object. Cache results you re-serialise anyway — handler
  returns, API payloads — rather than rich objects you need back by type.

## Backends

[`InMemoryCache`](../reference/tasks.md#veloce.InMemoryCache) is process-local and
size-bounded (`max_entries`, default 1024, evicting expired then oldest entries).
It is not shared across workers.

For a cache shared across every worker and host, use
[`RedisCache`](databases.md#redis-sessions-and-rate-limiting) from
`veloce.contrib.redis` (`pip install veloceframework[redis]`):

```python
from redis.asyncio import Redis

from veloce import Veloce, cached
from veloce.contrib.redis import RedisCache

app = Veloce()
cache = RedisCache(Redis.from_url("redis://localhost:6379/0"))


@app.get("/reports/{report_id}")
@cached(cache, ttl=60)
async def report(report_id: int) -> dict:
    return build_expensive_report(report_id)
```

Both backends satisfy the same `Cache` interface, so swapping one for the other
never changes behaviour — write your own backend by subclassing `Cache` and
implementing `get` / `set` / `delete`.

!!! note "Caching is opt-in and zero-cost when unused"
    Nothing in the request dispatch path references the cache. Adding the
    feature, or leaving handlers undecorated, has no effect on throughput.

## What's next

- [Databases](databases.md) — the Redis backend and session/rate-limit helpers
- [Dependency Injection](dependency-injection.md) — `cached` composes with `Depends`
