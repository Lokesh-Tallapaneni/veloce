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
from veloce.middleware.conditional import ConditionalGetMiddleware
from veloce.middleware.cors import CORSMiddleware
from veloce.middleware.csrf import CSRFMiddleware, rotate_csrf_token
from veloce.middleware.logging import LoggingMiddleware, RequestIDMiddleware
from veloce.middleware.proxy_fix import ProxyFix
from veloce.middleware.security import (
    CSPMiddleware,
    HTTPSRedirectMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    TrustedHostMiddleware,
    WebSocketOriginMiddleware,
    csp_nonce,
)
from veloce.middleware.sessions import (
    ServerSessionMiddleware,
    SessionMiddleware,
    SessionMiddlewareBase,
)

__all__ = [
    "Middleware",
    "BaseHTTPMiddleware",
    "CORSMiddleware",
    "CSRFMiddleware",
    "rotate_csrf_token",
    "ConditionalGetMiddleware",
    "GZipMiddleware",
    "TrustedHostMiddleware",
    "RateLimitMiddleware",
    "HTTPSRedirectMiddleware",
    "SecurityHeadersMiddleware",
    "CSPMiddleware",
    "csp_nonce",
    "WebSocketOriginMiddleware",
    "LoggingMiddleware",
    "RequestIDMiddleware",
    "SessionMiddleware",
    "ServerSessionMiddleware",
    "SessionMiddlewareBase",
    "ProxyFix",
]
