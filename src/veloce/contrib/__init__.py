"""Contrib sub-package — optional integrations (templating, OpenAPI, static files)."""

from __future__ import annotations

from veloce.contrib.openapi import get_openapi_schema, setup_openapi_routes
from veloce.contrib.redis import RedisCache, RedisRateLimitBackend, RedisSessionStore
from veloce.contrib.staticfiles import StaticFiles
from veloce.contrib.templating import (
    Jinja2Templates,
    render_template,
    render_template_string,
    stream_template,
)

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
