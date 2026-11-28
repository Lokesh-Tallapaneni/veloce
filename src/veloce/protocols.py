"""Pluggable-backend protocols — the structural contract each extension point asks for.

Every extension point Veloce lets an application replace (the result cache, the
server-side session store, the rate-limit state store, the JSON provider) is
described here as a `runtime_checkable` `Protocol`. A class satisfies one by
having the methods, with no inheritance required, so `isinstance(obj, X)` works
for the shipped classes and for a user's own. The shipped base classes (`Cache`,
`SessionStore`, `RateLimitBackend`, `JSONProvider`) stay as reference
implementations that fail at definition when a method is missing.

The MCP and plugin protocols live beside the code that drives them and are
re-exported here on first access, so importing this module does not load the
MCP subpackage or the app.
"""

from __future__ import annotations

import importlib
import inspect
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from veloce.exceptions import BackendError

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.response import Response
    from veloce.ratelimit import RateLimitResult, RateLimitStrategy


@runtime_checkable
class CacheBackend(Protocol):
    """What `cached` needs from a result-cache store: bytes in, bytes out, TTL in seconds."""

    async def get(self, key: str) -> bytes | None:
        """Return the cached bytes for `key`, or `None` when absent or expired."""
        ...

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        """Store `value` under `key` for `ttl` seconds."""
        ...

    async def delete(self, key: str) -> None:
        """Remove `key`; a missing key is not an error."""
        ...


@runtime_checkable
class SessionStoreBackend(Protocol):
    """What `ServerSessionMiddleware` needs from a server-side session store."""

    async def read(self, session_id: str) -> dict[str, Any] | None:
        """Return the stored session data, or `None` when unknown or expired."""
        ...

    async def write(self, session_id: str, data: dict[str, Any], max_age: int) -> None:
        """Persist `data` for `max_age` seconds."""
        ...

    async def delete(self, session_id: str) -> None:
        """Remove the session; an unknown id is not an error."""
        ...


@runtime_checkable
class RateLimitStoreBackend(Protocol):
    """What the rate-limit middleware needs from a state store: one atomic evaluate."""

    async def evaluate(self, key: str, strategy: RateLimitStrategy, now: float) -> RateLimitResult:
        """Apply `strategy` to the state held for `key` at time `now`, atomically."""
        ...


@runtime_checkable
class JSONProviderBackend(Protocol):
    """What the application needs from a JSON provider: bytes out, any JSON in."""

    def dumps(self, obj: Any, **kwargs: Any) -> bytes:
        """Serialise `obj` to JSON bytes."""
        ...

    def loads(self, data: bytes | str) -> Any:
        """Parse JSON text or bytes into Python values."""
        ...

    def response(self, value: Any, **kwargs: Any) -> Response:
        """Wrap `value` in a JSON response."""
        ...


def _require_backend(
    obj: Any, protocol: type, *, role: str, names: tuple[str, ...] | None = None
) -> None:
    """Refuse `obj` at registration unless it provides what `protocol` asks for.

    A type checker proves a custom backend has the right methods; this proves it
    to an application that is not type-checked, at the moment the backend is
    handed over rather than on the first live request that reaches a missing
    method. Beyond presence it checks the one thing a runtime `isinstance` against
    a protocol cannot: a method the protocol declares `async def` must be a
    coroutine function, because the framework `await`s it and a plain function
    would fail there with a confusing `TypeError` about awaiting a non-awaitable.

    Args:
        obj: The backend instance being registered.
        protocol: The protocol it must satisfy.
        role: What the backend is for, used in the message ("cache", "session store").
        names: Check only these members, for a protocol that declares optional extras.

    Raises:
        BackendError: If a member is missing or not callable, or is not `async def`
            where the protocol declares it so. It is also a `TypeError`.

    """
    members = {
        name: member
        for name, member in vars(protocol).items()
        if inspect.isfunction(member) and not name.startswith("_")
    }
    missing: list[str] = []
    not_async: list[str] = []
    for name in names if names is not None else tuple(members):
        attribute = getattr(obj, name, None)
        if not callable(attribute):
            missing.append(name)
        elif inspect.iscoroutinefunction(members[name]) and not inspect.iscoroutinefunction(
            attribute
        ):
            not_async.append(name)
    if missing or not_async:
        problems = []
        if missing:
            problems.append(f"missing {', '.join(missing)}")
        if not_async:
            problems.append(f"{', '.join(not_async)} must be `async def`")
        raise BackendError(
            f"the {role} {type(obj).__name__} does not satisfy {protocol.__name__}: "
            f"{'; '.join(problems)}"
        )


if TYPE_CHECKING:  # pragma: no cover

    def _conformance(app: Any) -> None:
        """Make the type checker prove every shipped backend satisfies its protocol.

        Never called and absent at runtime. A change to a shipped backend that
        drifts from the signature its protocol asks for fails `mypy` here,
        rather than being found by a user who passes it to an extension point.
        """
        from veloce.cache import InMemoryCache
        from veloce.contrib.redis import RedisCache, RedisRateLimitBackend, RedisSessionStore
        from veloce.json_provider import DefaultJSONProvider
        from veloce.ratelimit import InMemoryRateLimitBackend
        from veloce.sessions import InMemorySessionStore

        cache: CacheBackend = InMemoryCache()
        redis_cache: CacheBackend = RedisCache(app)
        store: SessionStoreBackend = InMemorySessionStore()
        redis_store: SessionStoreBackend = RedisSessionStore(app)
        limiter: RateLimitStoreBackend = InMemoryRateLimitBackend()
        redis_limiter: RateLimitStoreBackend = RedisRateLimitBackend(app)
        provider: JSONProviderBackend = DefaultJSONProvider(app)
        del cache, redis_cache, store, redis_store, limiter, redis_limiter, provider


# Protocols that live beside the code that drives them.
_REEXPORTS = {
    "Plugin": "veloce.app.plugins",
    "AuthorizationStore": "veloce.contrib.mcp.authorization",
    "UpstreamOAuthProxyStore": "veloce.contrib.mcp.oauth_proxy",
    "Transport": "veloce.contrib.mcp.transports.base",
    "BidirectionalTransport": "veloce.contrib.mcp.transports.base",
    "SessionBackend": "veloce.contrib.mcp.transports.session_store",
}


def __getattr__(name: str) -> Any:
    module = _REEXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_REEXPORTS})
