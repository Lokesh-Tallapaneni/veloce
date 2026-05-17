"""Middleware sub-package — base class and all built-in middleware."""

from veloce.middleware.base import BaseHTTPMiddleware, Middleware
from veloce.middleware.compression import GZipMiddleware
from veloce.middleware.cors import CORSMiddleware
from veloce.middleware.csrf import CSRFMiddleware
from veloce.middleware.logging import LoggingMiddleware, RequestIDMiddleware
from veloce.middleware.proxy_fix import ProxyFix
from veloce.middleware.security import (
    HTTPSRedirectMiddleware,
    RateLimitMiddleware,
    TrustedHostMiddleware,
)
from veloce.middleware.sessions import SessionMiddleware

__all__ = [
    "Middleware",
    "BaseHTTPMiddleware",
    "CORSMiddleware",
    "CSRFMiddleware",
    "GZipMiddleware",
    "TrustedHostMiddleware",
    "RateLimitMiddleware",
    "HTTPSRedirectMiddleware",
    "LoggingMiddleware",
    "RequestIDMiddleware",
    "SessionMiddleware",
    "ProxyFix",
]
