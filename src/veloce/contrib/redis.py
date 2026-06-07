"""Redis-backed session store and rate limiter — shared state across workers.

The bundled `InMemorySessionStore` and `RateLimitMiddleware` keep their state in
one process, so a multi-worker deployment (`uvicorn --workers N`) gets per-worker
sessions and an effective rate limit of roughly ``N x`` the configured one. These
Redis-backed implementations share state across every worker and host.

`redis` is an optional dependency; install it with ``pip install
veloceframework[redis]``. The application owns the client and its connection
pool - construct a ``redis.asyncio.Redis`` (or pass a URL to
``RedisSessionStore.from_url`` / ``RedisRateLimiter.from_url``) and hand it in.

Usage::

    from redis.asyncio import Redis

    from veloce import Veloce
    from veloce.contrib.redis import RedisRateLimiter, RedisSessionStore
    from veloce.middleware import ServerSessionMiddleware

    client = Redis.from_url("redis://localhost:6379/0")
    app = Veloce()
    app.add_middleware(ServerSessionMiddleware, store=RedisSessionStore(client))
    app.add_middleware(RedisRateLimiter(client, max_requests=100, window_seconds=60))
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import orjson

from veloce.cache import Cache
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware
from veloce.sessions import SessionStore
from veloce.status import HTTP_429_TOO_MANY_REQUESTS

if TYPE_CHECKING:  # pragma: no cover
    from redis.asyncio import Redis

# RFC 9110 section 10.2.3 - tells a throttled client when to retry.
_HEADER_RETRY_AFTER = "Retry-After"
# RateLimit response headers (draft-ietf-httpapi-ratelimit-headers), matching the
# in-process `RateLimitMiddleware` so a client sees the same surface either way.
_HEADER_LIMIT = "X-RateLimit-Limit"
_HEADER_REMAINING = "X-RateLimit-Remaining"
_HEADER_RESET = "X-RateLimit-Reset"


def _load_redis_from_url(url: str, **kwargs: Any) -> Redis:
    """Build a ``redis.asyncio.Redis`` from a URL, with an install hint."""
    try:
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover - exercised via the install hint
        raise RuntimeError(
            "RedisSessionStore/RedisRateLimiter need the 'redis' package. "
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


class RedisRateLimiter(Middleware):
    """Cross-worker fixed-window rate limiter backed by Redis.

    Unlike the in-process `RateLimitMiddleware`, the counter lives in Redis, so
    every worker and host enforces one shared limit. It is a fixed-window
    counter: each client gets at most ``max_requests`` per ``window_seconds``,
    and the window resets on a wall-clock boundary (a burst spanning a boundary
    can briefly admit up to ``2 x max_requests`` - the standard fixed-window
    trade-off; use a reverse-proxy limiter when stricter smoothing is required).

    The per-window counter is a single ``INCR`` with an ``EXPIRE`` set on first
    use, so each check is one round trip (two on the first request of a window).

    Usage::

        from redis.asyncio import Redis

        from veloce.contrib.redis import RedisRateLimiter

        client = Redis.from_url("redis://localhost:6379/0")
        app.add_middleware(RedisRateLimiter(client, max_requests=100, window_seconds=60))
    """

    def __init__(
        self,
        client: Redis,
        max_requests: int = 100,
        window_seconds: int = 60,
        *,
        prefix: str = "veloce:ratelimit:",
        name: str | None = None,
    ) -> None:
        super().__init__(name=name)
        if max_requests < 1:
            raise ValueError("RedisRateLimiter max_requests must be >= 1")
        if window_seconds < 1:
            raise ValueError("RedisRateLimiter window_seconds must be >= 1")
        self._redis = client
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._prefix = prefix

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        max_requests: int = 100,
        window_seconds: int = 60,
        prefix: str = "veloce:ratelimit:",
        name: str | None = None,
        **connection_kwargs: Any,
    ) -> RedisRateLimiter:
        """Build the limiter from a Redis URL, creating the client for you.

        Limiter options are explicit keywords; any extra keyword (``password``,
        ``ssl``, ...) is forwarded to ``redis.asyncio.Redis.from_url``.
        """
        return cls(
            _load_redis_from_url(url, **connection_kwargs),
            max_requests=max_requests,
            window_seconds=window_seconds,
            prefix=prefix,
            name=name,
        )

    def _client_key(self, request: Request) -> str:
        # The direct peer address; behind a proxy, pair with `ProxyFix` so
        # `client_host` is the real client rather than the proxy.
        return request.client_host or "unknown"

    async def process_request(self, request: Request) -> Response | None:
        """Reject the request with 429 once the per-window count is exceeded."""
        window = int(time.time()) // self.window_seconds
        key = f"{self._prefix}{self._client_key(request)}:{window}"

        # Create the per-window counter carrying its TTL before incrementing, so
        # the key always has an expiry (it can never leak) even if a crash lands
        # between the two commands. `SET ... NX EX` is a no-op once the window key
        # exists, so steady-state cost is the create-once plus one `INCR`.
        await self._redis.set(key, 0, ex=self.window_seconds, nx=True)
        count = await self._redis.incr(key)

        if count > self.max_requests:
            reset = (window + 1) * self.window_seconds - int(time.time())
            if reset < 1:
                reset = 1
            rejected = Response(
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                body=b"Too Many Requests",
                headers={
                    _HEADER_RETRY_AFTER: str(reset),
                    _HEADER_LIMIT: str(self.max_requests),
                    _HEADER_REMAINING: "0",
                    _HEADER_RESET: str(reset),
                },
            )
            return rejected
        return None


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
