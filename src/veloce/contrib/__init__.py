"""Contrib sub-package — optional integrations (templating, OpenAPI, static files).

Names are resolved on first attribute access rather than at import, so an
optional integration is not imported until one of its names is used.
`from veloce.contrib import X` is unchanged; only the moment the work happens
moves.

Two of the four are deferred in practice: OpenAPI and Redis. Static files and
templating are still pulled in by `import veloce` itself, because the top-level
package imports `contrib.templating` for its own re-exports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.openapi import get_openapi_schema, setup_openapi_routes
    from veloce.contrib.redis import RedisCache, RedisRateLimitBackend, RedisSessionStore
    from veloce.contrib.staticfiles import StaticFiles
    from veloce.contrib.templating import (
        Jinja2Templates,
        render_template,
        render_template_string,
        stream_template,
    )

# Each exported name and the module that defines it. Resolved once per name per
# process by `__getattr__` below - never on a per-request path.
_EXPORTS: dict[str, str] = {
    "Jinja2Templates": "veloce.contrib.templating",
    "RedisCache": "veloce.contrib.redis",
    "RedisRateLimitBackend": "veloce.contrib.redis",
    "RedisSessionStore": "veloce.contrib.redis",
    "StaticFiles": "veloce.contrib.staticfiles",
    "get_openapi_schema": "veloce.contrib.openapi",
    "render_template": "veloce.contrib.templating",
    "render_template_string": "veloce.contrib.templating",
    "setup_openapi_routes": "veloce.contrib.openapi",
    "stream_template": "veloce.contrib.templating",
}

__all__ = [
    "Jinja2Templates",
    "RedisCache",
    "RedisRateLimitBackend",
    "RedisSessionStore",
    "StaticFiles",
    "get_openapi_schema",
    "render_template",
    "render_template_string",
    "setup_openapi_routes",
    "stream_template",
]


def __getattr__(name: str) -> Any:
    """Import the module owning `name` on first access, then cache it here."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
