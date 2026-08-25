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

!!! warning "`cached` deduplicates in time, not across concurrent callers"
    A second call *after* the first has stored its result is served from the
    cache. Calls that arrive while the first is still running are not: each one
    misses, and each one runs the function.

    ```python
    # Ten requests arriving together on a cold key run `build` ten times.
    await asyncio.gather(*[build(1) for _ in range(10)])
    ```

    Every caller still gets a correct value and the cache converges on one
    entry — but if the work behind the cache is expensive enough that a burst of
    concurrent misses matters (a "stampede"), do the lookup yourself and
    re-check inside the lock:

    ```python
    lock = asyncio.Lock()


    async def build(n: int) -> dict:
        key = f"build:{n}"
        hit = await cache.get(key)
        if hit is not None:
            return hit
        async with lock:
            # Re-check: another task may have filled it while we queued.
            hit = await cache.get(key)
            if hit is not None:
                return hit
            value = await expensive(n)
            await cache.set(key, value, ttl=60)
            return value
    ```

    Ten concurrent callers run `expensive` once. Note the re-check is what does
    the work — wrapping a `@cached` function's *body* in a lock only serialises
    the callers, because the cache lookup has already happened by then, and all
    ten still run.

    Single-flight is not built in because it would put a lock acquisition on
    every lookup, including the hits, which is the common case.

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

All three are required, and a subclass that omits one is refused where it is
written rather than on the request that first needs it:

```python
class MyCache(Cache):
    __slots__ = ()

    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ttl: int) -> None: ...
    # `delete` forgotten

# TypeError: MyCache does not implement Cache: delete missing
```

Subclassing a *concrete* backend is the usual way to specialise one, and
inherits real implementations, so `class Instrumented(InMemoryCache)` needs
override nothing. `SessionStore` enforces its `read` / `write` / `delete` the
same way — see [Sessions](sessions.md).

!!! note "Added in version 0.18.0"
    The subclass check. A backend that already implements all three is
    unaffected.

!!! note "Caching is opt-in and zero-cost when unused"
    Nothing in the request dispatch path references the cache. Adding the
    feature, or leaving handlers undecorated, has no effect on throughput.

## What's next

- [Databases](databases.md) — the Redis backend and session/rate-limit helpers
- [Dependency Injection](dependency-injection.md) — `cached` composes with `Depends`
