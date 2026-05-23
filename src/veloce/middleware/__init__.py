"""Middleware sub-package — base class and all built-in middleware."""

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
