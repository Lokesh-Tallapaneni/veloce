"""Redis-backed session store, rate-limit backend, and result cache — shared state.

The bundled `InMemorySessionStore`, `RateLimitMiddleware`, and `InMemoryCache`
keep their state in one process, so a multi-worker deployment (`uvicorn --workers
N`) gets per-worker sessions, an effective rate limit of roughly ``N x`` the
configured one, and a per-worker cache. These Redis-backed implementations share
state across every worker and host.

`redis` is an optional dependency; install it with ``pip install
veloceframework[redis]``. The application owns the client and its connection
pool - construct a ``redis.asyncio.Redis`` (or pass a URL to a ``from_url``
classmethod) and hand it in.

Usage::

    from redis.asyncio import Redis

    from veloce import RateLimitMiddleware, TokenBucket, Veloce
    from veloce.contrib.redis import RedisRateLimitBackend, RedisSessionStore
    from veloce.middleware import ServerSessionMiddleware

    client = Redis.from_url("redis://localhost:6379/0")
    app = Veloce()
    app.add_middleware(ServerSessionMiddleware, store=RedisSessionStore(client))
    app.add_middleware(
        RateLimitMiddleware(
            strategy=TokenBucket(rate=100, per=60),
            backend=RedisRateLimitBackend(client),
        )
    )
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import orjson

from veloce.cache import Cache
from veloce.ratelimit import RateLimitBackend, RateLimitResult, RateLimitStrategy
from veloce.sessions import SessionStore

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

_logger = logging.getLogger(__name__)

# Cap the optimistic-lock retries so a single very hot key under heavy
# multi-connection contention cannot livelock a coroutine; past this the backend
# degrades to one best-effort non-transactional update.
_MAX_WATCH_RETRIES = 8


def _load_redis_from_url(url: str, **kwargs: Any) -> Redis:
    """Build a ``redis.asyncio.Redis`` from a URL, with an install hint."""
    try:
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover - exercised via the install hint
        raise RuntimeError(
            "The Redis-backed contrib helpers need the 'redis' package. "
            "Install it with: pip install veloceframework[redis]"
        ) from exc
    return Redis.from_url(url, **kwargs)


class RedisSessionStore(SessionStore):
    """A `SessionStore` backed by Redis, shared across workers and hosts.

    Payloads are stored as JSON under ``<prefix><session_id>`` with a native
    Redis TTL, so expiry, sliding-renewal (`touch`), and the race-safe
    conditional write (`replace`) use Redis primitives rather than the default
    read-then-write. The application owns the ``redis.asyncio.Redis`` client and
    its connection pool.

    Usage::

        from redis.asyncio import Redis

        from veloce.contrib.redis import RedisSessionStore

        store = RedisSessionStore(Redis.from_url("redis://localhost:6379/0"))
    """

    __slots__ = ("_redis", "_prefix")

    def __init__(self, client: Redis, *, prefix: str = "veloce:session:") -> None:
        self._redis = client
        self._prefix = prefix

    @classmethod
    def from_url(
        cls, url: str, *, prefix: str = "veloce:session:", **kwargs: Any
    ) -> RedisSessionStore:
        """Build a store from a Redis URL, creating the client for you."""
        return cls(_load_redis_from_url(url, **kwargs), prefix=prefix)

    def _key(self, session_id: str) -> str:
        return self._prefix + session_id

    async def read(self, session_id: str) -> dict[str, Any] | None:
        raw = await self._redis.get(self._key(session_id))
        if raw is None:
            return None
        return orjson.loads(raw)

    async def write(self, session_id: str, data: dict[str, Any], max_age: int) -> None:
        await self._redis.set(self._key(session_id), orjson.dumps(data), ex=max_age)

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))

    async def replace(self, session_id: str, data: dict[str, Any], max_age: int) -> bool:
        # `SET ... XX` writes only if the key still exists, so a session a
        # concurrent `delete` removed is never resurrected - atomic in Redis,
        # closing the check-then-write window the default implementation has.
        result = await self._redis.set(
            self._key(session_id), orjson.dumps(data), ex=max_age, xx=True
        )
        return bool(result)

    async def touch(self, session_id: str, max_age: int) -> bool:
        # `EXPIRE` refreshes the TTL without moving the payload and returns
        # whether the key existed - exactly the sliding-expiry primitive.
        return bool(await self._redis.expire(self._key(session_id), max_age))


class RedisRateLimitBackend(RateLimitBackend):
    """A `RateLimitBackend` backed by Redis, shared across workers and hosts.

    Pair it with any strategy (`FixedWindow`, `SlidingWindow`, `TokenBucket`) on
    `RateLimitMiddleware` to enforce one limit across every worker and host,
    rather than the per-worker count of the default `InMemoryRateLimitBackend`.

    A built-in strategy runs as a Lua script: the whole read-modify-write executes
    inside Redis, atomically, in **one round trip** rather than the three a
    ``WATCH``/``MULTI`` transaction costs. There is no watch to lose, so no retry
    loop, and no contended-key fallback that abandons atomicity - which mattered,
    because a rate limiter's hot key is contended by definition.

    A strategy defined outside Veloce has no Lua form and keeps the
    ``WATCH``/``MULTI`` path: the state is read under a watch, the pure Python
    `evaluate` computes the next state, and the write commits only if no
    concurrent request changed the key, otherwise it retries. Declaring
    `lua_script` on a custom strategy opts it into the faster path.

    State is stored as JSON under ``<prefix><key>`` with the strategy's TTL, so
    idle clients expire on their own, and the two forms write the same shape - a
    rolling upgrade can run both against one key.

    Usage::

        from redis.asyncio import Redis

        from veloce import RateLimitMiddleware, TokenBucket
        from veloce.contrib.redis import RedisRateLimitBackend

        client = Redis.from_url("redis://localhost:6379/0")
        app.add_middleware(
            RateLimitMiddleware(
                strategy=TokenBucket(rate=100, per=60),
                backend=RedisRateLimitBackend(client),
            )
        )
    """

    __slots__ = ("_redis", "_prefix", "_watch_error", "_no_script_error", "_digests")

    def __init__(self, client: Redis, *, prefix: str = "veloce:ratelimit:") -> None:
        # redis is present whenever this backend is constructed; importing the
        # exception class here (not per request) keeps the module importable
        # without redis installed while giving the retry loop a concrete type.
        from redis.exceptions import NoScriptError, WatchError

        self._redis = client
        self._prefix = prefix
        self._watch_error = WatchError
        self._no_script_error = NoScriptError
        # script text -> SHA1, so a repeat call is one `EVALSHA` round trip.
        self._digests: dict[str, str] = {}

    @classmethod
    def from_url(
        cls, url: str, *, prefix: str = "veloce:ratelimit:", **kwargs: Any
    ) -> RedisRateLimitBackend:
        """Build the backend from a Redis URL, creating the client for you."""
        return cls(_load_redis_from_url(url, **kwargs), prefix=prefix)

    async def evaluate(self, key: str, strategy: RateLimitStrategy, now: float) -> RateLimitResult:
        redis_key = self._prefix + key
        script = strategy.lua_script
        if script is not None:
            return await self._evaluate_lua(redis_key, strategy, script, now)
        return await self._evaluate_watched(redis_key, strategy, now)

    async def _evaluate_lua(
        self, redis_key: str, strategy: RateLimitStrategy, script: str, now: float
    ) -> RateLimitResult:
        """Run the strategy inside Redis, in one round trip.

        A Lua script is the whole read-modify-write, executed atomically by the
        server: no `WATCH` to lose, no retries, and no contended-key fallback that
        gives up on atomicity. That fallback mattered - a rate limiter's hot key
        is contended *by definition*, and past the retry budget the watched path
        admits requests over the limit.

        The digest is cached and `EVALSHA` used; a server that has not seen the
        script (a restart, a failover, `SCRIPT FLUSH`) answers `NOSCRIPT`, and it
        is loaded and retried once.
        """
        digest = self._digests.get(script)
        argv = strategy.lua_argv(now)
        if digest is None:
            digest = await self._redis.script_load(script)
            self._digests[script] = digest
        try:
            raw = await self._redis.evalsha(digest, 1, redis_key, *argv)
        except self._no_script_error:
            # The server forgot the script - a restart, a failover, or a
            # `SCRIPT FLUSH`. Reload and retry once. Matched on the exception
            # class rather than the message: redis-py raises `NoScriptError`,
            # whose text is "No matching script", so a substring test for
            # "NOSCRIPT" silently never fired.
            digest = await self._redis.script_load(script)
            self._digests[script] = digest
            raw = await self._redis.evalsha(digest, 1, redis_key, *argv)
        allowed, limit, remaining, retry_after, reset = (int(v) for v in raw)
        return RateLimitResult(bool(allowed), limit, remaining, retry_after, reset)

    async def _evaluate_watched(
        self, redis_key: str, strategy: RateLimitStrategy, now: float
    ) -> RateLimitResult:
        """Run a strategy that has no Lua form, under optimistic locking.

        The path a strategy defined outside Veloce takes: `evaluate` is Python, so
        the read-modify-write happens here and `WATCH` guards it.
        """
        async with self._redis.pipeline() as pipe:
            for _ in range(_MAX_WATCH_RETRIES):
                try:
                    await pipe.watch(redis_key)
                    raw = await pipe.get(redis_key)
                    state = orjson.loads(raw) if raw is not None else None
                    result, new_state, ttl = strategy.evaluate(state, now)
                    pipe.multi()
                    pipe.set(redis_key, orjson.dumps(new_state), ex=ttl)
                    await pipe.execute()
                    return result
                except self._watch_error:
                    # A concurrent request changed the key between WATCH and
                    # EXEC; re-read and recompute against the fresh state.
                    continue
        # Sustained contention on this key exhausted the atomic retries. Fall back
        # to one non-transactional read-modify-write (last-writer-wins) so the
        # request is served instead of livelocking; the limit may be exceeded by
        # the number of racing writers in this rare window.
        _logger.warning("rate-limit WATCH contention on %r; using non-atomic fallback", redis_key)
        raw = await self._redis.get(redis_key)
        state = orjson.loads(raw) if raw is not None else None
        result, new_state, ttl = strategy.evaluate(state, now)
        await self._redis.set(redis_key, orjson.dumps(new_state), ex=ttl)
        return result


class RedisCache(Cache):
    """A `Cache` backed by Redis, shared across workers and hosts.

    Stores the value bytes under ``<prefix><key>`` with a native Redis TTL, so
    `veloce.cache.cached` shares one cache across every worker - unlike the
    process-local `InMemoryCache`. The application owns the client and its pool.

    Usage::

        from redis.asyncio import Redis

        from veloce.cache import cached
        from veloce.contrib.redis import RedisCache

        cache = RedisCache(Redis.from_url("redis://localhost:6379/0"))

        @app.get("/reports/{report_id}")
        @cached(cache, ttl=60)
        async def report(report_id: int) -> dict: ...
    """

    __slots__ = ("_redis", "_prefix")

    def __init__(self, client: Redis, *, prefix: str = "veloce:cache:") -> None:
        self._redis = client
        self._prefix = prefix

    @classmethod
    def from_url(cls, url: str, *, prefix: str = "veloce:cache:", **kwargs: Any) -> RedisCache:
        """Build a cache from a Redis URL, creating the client for you."""
        return cls(_load_redis_from_url(url, **kwargs), prefix=prefix)

    async def get(self, key: str) -> bytes | None:
        # The redis stub widens get() to `bytes | str | None` (a client may set
        # decode_responses=True); this cache stores and reads bytes.
        return await self._redis.get(self._prefix + key)  # type: ignore[return-value]

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        await self._redis.set(self._prefix + key, value, ex=ttl)

    async def delete(self, key: str) -> None:
        await self._redis.delete(self._prefix + key)
