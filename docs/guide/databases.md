---
description: Connect a database to Veloce — async SQLAlchemy with connection pooling and a per-request session dependency, plus Redis-backed sessions and rate limiting.
---

# Databases

Veloce does not ship an ORM. Like FastAPI and Starlette, it stays a focused
web layer and pairs with whatever data library you prefer — most commonly
[SQLAlchemy](https://www.sqlalchemy.org/) (async) for SQL, or a driver like
`asyncpg` directly. This page shows the recommended pattern: create one pooled
engine for the app's lifetime and inject a per-request session with `Depends`.

## Async SQLAlchemy with a connection pool

Install SQLAlchemy and an async driver:

```bash
pip install "sqlalchemy[asyncio]" asyncpg
```

Create the engine once at startup and dispose it at shutdown, so the connection
pool is shared by every request rather than rebuilt per call:

```python
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from veloce import Depends, Request, Veloce

app = Veloce()


@app.on_startup
async def open_database():
    # `pool_size` + `max_overflow` bound the connections this process holds;
    # size the total across all workers under your database's connection limit.
    engine = create_async_engine(
        "postgresql+asyncpg://user:pass@localhost/app",
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
    )
    app.state.db_engine = engine
    app.state.db_sessionmaker = async_sessionmaker(engine, expire_on_commit=False)


@app.on_shutdown
async def close_database():
    await app.state.db_engine.dispose()
```

Inject a session per request with a `yield` dependency, so it is always closed
(and rolled back on error) after the handler returns:

```python
async def get_session() -> AsyncIterator[AsyncSession]:
    async with app.state.db_sessionmaker() as session:
        yield session


@app.get("/users/{user_id}")
async def read_user(user_id: int, session: AsyncSession = Depends(get_session)):
    user = await session.get(User, user_id)
    if user is None:
        return {"detail": "not found"}, 404
    return {"id": user.id, "name": user.name}
```

The pool lives for the process lifetime; each request borrows a connection for
the duration of its session and returns it on close. Do **not** create an engine
per request — that defeats pooling and exhausts the database's connection slots.

!!! warning "Size the pool for your worker count"
    Each worker process holds its own pool. With `uvicorn --workers N` the
    cluster can open up to `N x (pool_size + max_overflow)` connections —
    keep that product under your database's `max_connections`.

## Migrations

There is no built-in migration tool. Use [Alembic](https://alembic.sqlalchemy.org/)
(SQLAlchemy's companion) to version your schema — it runs as a standalone CLI and
needs no Veloce integration.

## Redis: sessions and rate limiting

For state that must be **shared across workers and hosts** — server-side
sessions and rate-limit counters — Veloce ships Redis-backed implementations in
`veloce.contrib.redis`. Install the backend:

```bash
pip install veloceframework[redis]
```

The app owns the Redis client and its pool; hand it to either helper.

### Shared sessions

`RedisSessionStore` implements the
[`SessionStore`](../reference.md#veloce.SessionStore) interface using native
Redis TTLs for expiry, sliding renewal (`EXPIRE`), and a race-safe conditional
write (`SET ... XX`):

```python
from redis.asyncio import Redis

from veloce import ServerSessionMiddleware, Veloce
from veloce.contrib.redis import RedisSessionStore

app = Veloce()
client = Redis.from_url("redis://localhost:6379/0")
app.add_middleware(ServerSessionMiddleware, store=RedisSessionStore(client))
```

Unlike the default `InMemorySessionStore`, every worker reads and writes the same
sessions. See [Sessions](sessions.md) for the cookie options and the read/write
API.

!!! note "Session values must be JSON-serializable"
    `RedisSessionStore` stores payloads as JSON, so values must be
    JSON-serializable (the in-memory store keeps arbitrary Python objects). A
    `datetime`, for example, comes back as its ISO string — store primitives,
    lists, and dicts.

### Cross-worker rate limiting

`RateLimitMiddleware` chooses an algorithm with a `strategy` and where the
counter lives with a `backend`. `RedisRateLimitBackend` keeps the per-client
state in Redis, so the limit is enforced once across the whole cluster (the
default `InMemoryRateLimitBackend` counts per worker):

```python
from redis.asyncio import Redis

from veloce import RateLimitMiddleware, TokenBucket, Veloce
from veloce.contrib.redis import RedisRateLimitBackend

app = Veloce()
client = Redis.from_url("redis://localhost:6379/0")
app.add_middleware(
    RateLimitMiddleware(
        strategy=TokenBucket(rate=100, per=60),
        backend=RedisRateLimitBackend(client),
    )
)
```

Pick the algorithm that fits: `FixedWindow` (simplest), `SlidingWindow` (smooths
the window-boundary burst), or `TokenBucket` (allows a controlled burst up to
`burst`, default `rate`). Each runs identically on either backend. Behind a
proxy, pair it with `ProxyFix` so the limiter keys on the real client IP rather
than the proxy's.

See [Middleware](middleware.md#rate-limiting) for the full algorithm comparison.

## What's next

- [Sessions](sessions.md)
- [Middleware](middleware.md)
- [Deployment](deployment.md)
