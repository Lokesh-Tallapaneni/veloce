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
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.response import Response
    from veloce.ratelimit import RateLimitResult, RateLimitStrategy


@runtime_checkable
class CacheBackend(Protocol):
    """What `cached` needs from a result-cache store: bytes in, bytes out, TTL in seconds."""

    async def get(self, key: str) -> bytes | None: ...

    async def set(self, key: str, value: bytes, ttl: int) -> None: ...

    async def delete(self, key: str) -> None: ...


@runtime_checkable
class SessionStoreBackend(Protocol):
    """What `ServerSessionMiddleware` needs from a server-side session store."""

    async def read(self, session_id: str) -> dict[str, Any] | None: ...

    async def write(self, session_id: str, data: dict[str, Any], max_age: int) -> None: ...

    async def delete(self, session_id: str) -> None: ...


@runtime_checkable
class RateLimitStoreBackend(Protocol):
    """What the rate-limit middleware needs from a state store: one atomic evaluate."""

    async def evaluate(
        self, key: str, strategy: RateLimitStrategy, now: float
    ) -> RateLimitResult: ...


@runtime_checkable
class JSONProviderBackend(Protocol):
    """What the application needs from a JSON provider: bytes out, any JSON in."""

    def dumps(self, obj: Any, **kwargs: Any) -> bytes: ...

    def loads(self, data: bytes | str) -> Any: ...

    def response(self, value: Any, **kwargs: Any) -> Response: ...


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
