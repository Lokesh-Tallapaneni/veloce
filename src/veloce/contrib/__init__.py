"""Contrib sub-package — optional integrations (templating, OpenAPI, static files)."""

from __future__ import annotations

from veloce.contrib.openapi import get_openapi_schema, setup_openapi_routes
from veloce.contrib.redis import RedisRateLimiter, RedisSessionStore
from veloce.contrib.staticfiles import StaticFiles
from veloce.contrib.templating import Jinja2Templates

__all__ = [
    "Jinja2Templates",
    "RedisRateLimiter",
    "RedisSessionStore",
    "StaticFiles",
    "get_openapi_schema",
    "setup_openapi_routes",
]
