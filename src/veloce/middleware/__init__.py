"""Middleware — base class and all built-in middleware.

Middleware wraps the request/response cycle. Ordering is significant: the
outermost layer sees the request first and the response last. Register
security-related middleware (trusted host, HTTPS redirect, CORS, security
headers) outermost so they run before anything that inspects or mutates the
request body, with session/CSRF and observability (logging, request id)
layered inside.
"""

from __future__ import annotations

from veloce.middleware.base import BaseHTTPMiddleware, Middleware
from veloce.middleware.compression import GZipMiddleware
from veloce.middleware.cors import CORSMiddleware
from veloce.middleware.csrf import CSRFMiddleware, rotate_csrf_token
from veloce.middleware.logging import LoggingMiddleware, RequestIDMiddleware
from veloce.middleware.proxy_fix import ProxyFix
from veloce.middleware.security import (
    HTTPSRedirectMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    TrustedHostMiddleware,
    WebSocketOriginMiddleware,
)
from veloce.middleware.sessions import ServerSessionMiddleware, SessionMiddleware

__all__ = [
    "Middleware",
    "BaseHTTPMiddleware",
    "CORSMiddleware",
    "CSRFMiddleware",
    "rotate_csrf_token",
    "GZipMiddleware",
    "TrustedHostMiddleware",
    "RateLimitMiddleware",
    "HTTPSRedirectMiddleware",
    "SecurityHeadersMiddleware",
    "WebSocketOriginMiddleware",
    "LoggingMiddleware",
    "RequestIDMiddleware",
    "SessionMiddleware",
    "ServerSessionMiddleware",
    "ProxyFix",
]
