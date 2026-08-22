"""Result caching — opt-in memoisation that never touches the dispatch path.

`cached` wraps an async function and stores its JSON-serialisable return in a
`Cache` backend, keyed by the call arguments. It is a self-contained decorator:
a handler that is not decorated pays nothing, and the request pipeline imports
none of this, so adding the feature cannot slow an app that does not use it.

The bundled `InMemoryCache` is process-local and size-bounded;
`veloce.contrib.redis.RedisCache` shares entries across workers and hosts. Both
satisfy the same bytes-in/bytes-out `Cache` contract, so swapping backends never
changes behaviour - a cache hit always returns a freshly decoded value, never a
shared mutable object.

Usage::

    from veloce import InMemoryCache, Veloce, cached

    app = Veloce()
    cache = InMemoryCache()

    @app.get("/reports/{report_id}")
    @cached(cache, ttl=60)
    async def report(report_id: int) -> dict:
        return build_expensive_report(report_id)
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from hashlib import blake2b
from typing import Any

import orjson

from veloce._internal import _is_async_callable

# Stable digest input: sort dict keys so equal mappings hash identically
# regardless of construction order.
_KEY_OPTIONS = orjson.OPT_SORT_KEYS


def _ignore_unserialisable(_value: Any) -> None:
    # An argument the key cannot serialise (a Request, an injected Response, a
    # Depends result) collapses to null, so a handler is keyed only by its
    # scalar inputs and the injected request never makes two equivalent calls
    # miss each other.
    return None


def _default_key(func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Build a cache key from a function's qualified name and its arguments."""
    payload = orjson.dumps((args, kwargs), default=_ignore_unserialisable, option=_KEY_OPTIONS)
    return f"{func.__qualname__}:{blake2b(payload, digest_size=16).hexdigest()}"


def _cache_default(value: Any) -> Any:
    # Pydantic models serialise via their JSON dump; anything else orjson cannot
    # encode is a programming error (the result is not cacheable).
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    raise TypeError(f"cannot cache a value of type {type(value).__name__!r}: not JSON-serialisable")


class Cache:
    """Result-cache backend interface.

    A backend stores opaque ``bytes`` under a string key with a per-entry TTL in
    seconds. The methods are async so a network-backed store does not block the
    event loop. `cached` serialises and deserialises the values, so a backend
    only moves bytes.
    """

    __slots__ = ()

    async def get(self, key: str) -> bytes | None:
        """Return the stored bytes for `key`, or `None` if absent or expired."""
        raise NotImplementedError

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        """Store `value` under `key`, to expire after `ttl` seconds."""
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        """Remove `key` if present."""
        raise NotImplementedError


class InMemoryCache(Cache):
    """A process-local, size-bounded result cache with per-entry TTL.

    Fine for a single process and tests. It does not share state across workers,
    so a multi-worker deployment needs a shared backend such as
    `veloce.contrib.redis.RedisCache`. TTLs use a monotonic clock, so a wall-clock
    change cannot prematurely expire or extend an entry.

    Usage::

        from veloce import InMemoryCache

        cache = InMemoryCache(max_entries=2048)
    """

    __slots__ = ("_entries", "_max_entries")

    def __init__(self, max_entries: int = 1024) -> None:
        if max_entries < 1:
            raise ValueError("InMemoryCache max_entries must be >= 1")
        # key -> (value bytes, monotonic expiry)
        self._entries: dict[str, tuple[bytes, float]] = {}
        self._max_entries = max_entries

    async def get(self, key: str) -> bytes | None:
        """Return the stored bytes for `key`, or `None` if absent or expired."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= time.monotonic():
            # Evict lazily, on the access that observes the expiry.
            del self._entries[key]
            return None
        return value

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        """Store `value` under `key` for `ttl` seconds, evicting when at capacity."""
        if key not in self._entries and len(self._entries) >= self._max_entries:
            self._evict()
        self._entries[key] = (value, time.monotonic() + ttl)

    async def delete(self, key: str) -> None:
        """Remove `key` if present."""
        self._entries.pop(key, None)

    def _evict(self) -> None:
        # Drop expired entries first; if still at capacity, drop the oldest by
        # insertion order (dicts preserve it). This bounds memory without the
        # per-`get` bookkeeping a true LRU would add to the hot read path.
        now = time.monotonic()
        expired = [k for k, (_, expires_at) in self._entries.items() if expires_at <= now]
        for k in expired:
            del self._entries[k]
        if len(self._entries) >= self._max_entries:
            del self._entries[next(iter(self._entries))]


def cached(
    cache: Cache,
    *,
    ttl: int,
    key: Callable[..., str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Memoise an async function's JSON-serialisable return in `cache`.

    The key defaults to the function's qualified name plus a digest of its
    arguments (arguments that are not JSON-serialisable, such as an injected
    `Request`, are ignored), so a handler is cached by its scalar inputs. Pass
    `key=` a callable taking the same arguments for full control.

    The result must be JSON-serialisable (a Pydantic model is dumped in JSON
    mode); a non-serialisable result raises `TypeError`. A cache **hit returns
    the JSON-decoded value**, so cache results you will re-serialise (handler
    returns, API payloads) rather than rich objects you need back by type. Only
    async functions are supported.
    """
    if ttl < 1:
        raise ValueError("cached ttl must be >= 1 second")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        if not _is_async_callable(func):
            raise TypeError("cached can only wrap an async function")

        def build_key(*args: Any, **kwargs: Any) -> str:
            return _default_key(func, args, kwargs)

        key_for = key if key is not None else build_key

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            cache_key = key_for(*args, **kwargs)
            hit = await cache.get(cache_key)
            if hit is not None:
                return orjson.loads(hit)
            result = await func(*args, **kwargs)
            try:
                payload = orjson.dumps(result, default=_cache_default)
            except orjson.JSONEncodeError as err:
                # orjson wraps the `_cache_default` TypeError; re-raise a clean
                # one so the contract is "non-serialisable result -> TypeError".
                raise TypeError(
                    f"cannot cache a {type(result).__name__!r} result: not JSON-serialisable"
                ) from err
            await cache.set(cache_key, payload, ttl)
            return result

        return wrapper

    return decorator
