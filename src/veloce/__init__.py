"""Veloce — Ultra-fast async Python web framework.

Veloce is a high-performance asynchronous web framework built on raw asyncio,
httptools, and orjson. It pairs a small, well-typed API with predictable
performance under load.

Basic usage::

    from veloce import Veloce, Request

    app = Veloce()

    @app.get("/")
    async def index(request: Request):
        return {"message": "Hello, World!"}

    app.run()
"""

from __future__ import annotations

# Configuration
from typing import TYPE_CHECKING, Any

# Status codes
from veloce import status
from veloce._warnings import VeloceDeprecationWarning
from veloce.app import Plugin, URLRule, Veloce

# Background tasks
from veloce.background import BackgroundTask, BackgroundTasks
from veloce.blueprints import Blueprint
from veloce.cache import Cache, InMemoryCache, cached
from veloce.config import Config

# MCP (Model Context Protocol) - the per-call context handle a tool handler
# may declare. The server / transport classes stay under veloce.contrib.mcp.
# Resolved on first access rather than at import: reaching it eagerly
# initialises the whole MCP subpackage - server, registries, tasks, both
# transports - for one re-exported name, which every `import veloce` paid for
# whether or not the application exposes a single tool.
if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.context import MCPContext

# Static files
from veloce.contrib.staticfiles import StaticFiles

# Templating - top-level shortcuts. The full Jinja2Templates
# class stays under veloce.contrib.templating for callers that want the
# class-based API.
from veloce.contrib.templating import (
    Jinja2Templates,
    render_template,
    render_template_string,
    stream_template,
)

# Dependency injection
from veloce.dependency import Depends, Security, SecurityScopes

# Encoders
from veloce.encoders import jsonable_encoder, register_encoder, unregister_encoder

# Exceptions
from veloce.exceptions import (
    BadGateway,
    BadRequest,
    BuildError,
    ConfigurationError,
    Conflict,
    DuplicateRouteError,
    ExpectationFailed,
    FilesKeyError,
    Forbidden,
    GatewayTimeout,
    Gone,
    HTTPException,
    ImATeapot,
    InternalServerError,
    LengthRequired,
    MethodNotAllowed,
    NotAcceptable,
    NotFound,
    PaymentRequired,
    PreconditionFailed,
    ProxyAuthenticationRequired,
    RangeNotSatisfiable,
    RequestEntityTooLarge,
    RequestTimeout,
    RequestURITooLong,
    RequestValidationError,
    ServerNotImplemented,
    ServiceUnavailable,
    SetupError,
    TooManyRequests,
    Unauthorized,
    UnprocessableEntity,
    UnsupportedMediaType,
    ValidationError,
    VeloceError,
    WebSocketDisconnect,
    WebSocketException,
    WebSocketRequestValidationError,
    http_exception_handler,
    request_validation_exception_handler,
)

# Liveness / readiness endpoints. The Prometheus and OpenTelemetry
# instrumentors stay under veloce.metrics / veloce.otel - importing them costs
# ~72 ms and ~32 ms, which every user would otherwise pay at `import veloce`.
from veloce.health import HealthPlugin

# Helpers
from veloce.helpers import (
    Aborter,
    abort,
    after_this_request,
    async_send_file,
    current_app,
    flash,
    g,
    get_flashed_messages,
    has_app_context,
    has_request_context,
    jsonify,
    make_response,
    redirect,
    request,
    send_file,
    send_from_directory,
    send_from_directory_async,
    session,
    stream_with_context,
    url_for,
)

# Data structures
from veloce.http.datastructures import (
    URL,
    AcceptHeader,
    Address,
    Authorization,
    Cookies,
    FormData,
    Headers,
    QueryParams,
    RangeSpec,
    State,
    UploadFile,
)

# HTTP
from veloce.http.request import Request
from veloce.http.response import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    ORJSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
    UJSONResponse,
)
from veloce.instrumentation import RequestMetrics

# JSON provider
from veloce.json_provider import (
    DefaultJSONProvider,
    JSONProvider,
    config_orjson_options,
)

# HTML-safe strings
from veloce.markup import Markup, escape

# Middleware
from veloce.middleware import (
    BaseHTTPMiddleware,
    ConditionalGetMiddleware,
    CORSMiddleware,
    CSPMiddleware,
    CSRFMiddleware,
    GZipMiddleware,
    HTTPSRedirectMiddleware,
    LoggingMiddleware,
    Middleware,
    ProxyFix,
    RateLimitMiddleware,
    RequestIDMiddleware,
    SecurityHeadersMiddleware,
    ServerSessionMiddleware,
    SessionMiddleware,
    TrustedHostMiddleware,
    WebSocketOriginMiddleware,
    csp_nonce,
    rotate_csrf_token,
)

# Observability
from veloce.observability import instrument_access_log, log_requests_as_json

# Password hashing helpers
from veloce.passwords import (
    hash_password,
    hash_password_async,
    is_strong_password,
    needs_rehash,
    verify_and_needs_update,
    verify_and_needs_update_async,
    verify_password,
    verify_password_async,
)

# Principal (authenticated identity, shared across HTTP and MCP)
from veloce.principal import Principal, current_principal, set_principal

# Rate-limit algorithms and backends (selectable via RateLimitMiddleware)
from veloce.ratelimit import (
    FixedWindow,
    InMemoryRateLimitBackend,
    RateLimitBackend,
    RateLimitResult,
    RateLimitStrategy,
    SlidingWindow,
    TokenBucket,
    rate_limit,
)
from veloce.routing.converters import Converter, register_converter
from veloce.routing.params import Body, Cookie, File, Form, Header, Path, Query

# Routing
from veloce.routing.router import Router

# Filesystem-safety helpers
from veloce.safe import constant_time_compare, safe_join, secure_filename

# Secret wrapper
from veloce.secret import Secret

# Security
from veloce.security import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    BadResetToken,
    Claims,
    ExpiredSignatureError,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
    HTTPDigest,
    HTTPDigestCredentials,
    ImmatureSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidSignatureError,
    InvalidTokenError,
    JWTError,
    MissingClaimError,
    OAuth2AuthorizationCodeBearer,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
    OAuth2PasswordRequestFormStrict,
    OpenIdConnect,
    SecurityScheme,
    SessionAuth,
    UnsupportedAlgorithmError,
    check_reset_token,
    decode_jwt,
    encode_jwt,
    login_session,
    logout_session,
    make_reset_token,
)

# Sessions
from veloce.sessions import InMemorySessionStore, Session, SessionStore

# Signals
from veloce.signals import (
    Namespace,
    Signal,
    appcontext_popped,
    appcontext_pushed,
    appcontext_tearing_down,
    got_request_exception,
    message_flashed,
    request_finished,
    request_started,
    request_tearing_down,
)

# HMAC-signed value serialiser
from veloce.signing import BadData, BadSignature, BadTimeSignature, Signer

# Server-Sent Events
from veloce.sse import EventSourceResponse, ServerSentEvent

# Testing
from veloce.testclient import AsyncTestClient, TestClient

# Class-based views
from veloce.views import MethodView, View

# Event-loop watchdog
from veloce.watchdog import EventLoopWatchdog

# WebSocket
from veloce.websocket import WebSocket

try:
    from importlib.metadata import PackageNotFoundError as _PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("veloceframework")
    del _pkg_version, _PackageNotFoundError
except Exception:
    # Editable install before metadata is materialised, or an unsupported
    # runtime. The installed package metadata is the single source of the
    # version (`pyproject.toml`); fall back to a non-version sentinel rather
    # than a second hand-maintained literal so the two cannot drift.
    __version__ = "0.0.0+unknown"

# `APIRouter` aliases `Router`, whose constructor takes the keyword
# surface that name implies (`prefix=`, `tags=`, `dependencies=`,
# `responses=`). Route groups that need a name, hooks, or scoped error
# handlers use `Blueprint`; both register via `app.include_router`.
APIRouter = Router

__all__ = [
    # Core
    "Veloce",
    "Plugin",
    "Request",
    "Router",
    "Blueprint",
    "APIRouter",
    "URLRule",
    "Config",
    # Responses
    "Response",
    "JSONResponse",
    "ORJSONResponse",
    "UJSONResponse",
    "HTMLResponse",
    "PlainTextResponse",
    "RedirectResponse",
    "StreamingResponse",
    "FileResponse",
    "EventSourceResponse",
    # Middleware
    "Middleware",
    "BaseHTTPMiddleware",
    "CORSMiddleware",
    "CSRFMiddleware",
    "ConditionalGetMiddleware",
    "GZipMiddleware",
    "TrustedHostMiddleware",
    "RateLimitMiddleware",
    # Rate-limit algorithms and backends
    "RateLimitStrategy",
    "FixedWindow",
    "SlidingWindow",
    "TokenBucket",
    "RateLimitBackend",
    "InMemoryRateLimitBackend",
    "RateLimitResult",
    "rate_limit",
    "HTTPSRedirectMiddleware",
    "SecurityHeadersMiddleware",
    "CSPMiddleware",
    "csp_nonce",
    "WebSocketOriginMiddleware",
    "LoggingMiddleware",
    "RequestIDMiddleware",
    "SessionMiddleware",
    "ServerSessionMiddleware",
    "ProxyFix",
    "rotate_csrf_token",
    # Sessions
    "Session",
    "SessionStore",
    "InMemorySessionStore",
    # WebSocket
    "WebSocket",
    "WebSocketDisconnect",
    "WebSocketException",
    "WebSocketRequestValidationError",
    # DI
    "Depends",
    "Security",
    "SecurityScopes",
    # Exceptions
    "VeloceDeprecationWarning",
    "VeloceError",
    "HTTPException",
    "FilesKeyError",
    "ValidationError",
    "RequestValidationError",
    "BuildError",
    "ConfigurationError",
    "DuplicateRouteError",
    "SetupError",
    "http_exception_handler",
    "request_validation_exception_handler",
    # Named HTTP errors - 4xx
    "BadRequest",
    "Unauthorized",
    "PaymentRequired",
    "Forbidden",
    "NotFound",
    "MethodNotAllowed",
    "NotAcceptable",
    "ProxyAuthenticationRequired",
    "RequestTimeout",
    "Conflict",
    "Gone",
    "LengthRequired",
    "PreconditionFailed",
    "RequestEntityTooLarge",
    "RequestURITooLong",
    "UnsupportedMediaType",
    "RangeNotSatisfiable",
    "ExpectationFailed",
    "ImATeapot",
    "UnprocessableEntity",
    "TooManyRequests",
    # Named HTTP errors - 5xx
    "InternalServerError",
    "ServerNotImplemented",
    "BadGateway",
    "ServiceUnavailable",
    "GatewayTimeout",
    # Background
    "BackgroundTask",
    "BackgroundTasks",
    # Caching
    "Cache",
    "InMemoryCache",
    "cached",
    # Static
    "StaticFiles",
    # MCP (Model Context Protocol)
    "MCPContext",
    # Data structures
    "UploadFile",
    "URL",
    "FormData",
    "Headers",
    "QueryParams",
    "Cookies",
    "State",
    "Address",
    "Authorization",
    "AcceptHeader",
    "RangeSpec",
    # Security
    "SecurityScheme",
    "HTTPBasic",
    "HTTPBasicCredentials",
    "HTTPBearer",
    "HTTPDigest",
    "HTTPDigestCredentials",
    "APIKeyHeader",
    "APIKeyQuery",
    "APIKeyCookie",
    "SessionAuth",
    "login_session",
    "logout_session",
    "OAuth2PasswordBearer",
    "OAuth2PasswordRequestForm",
    "OAuth2PasswordRequestFormStrict",
    "OAuth2AuthorizationCodeBearer",
    "OpenIdConnect",
    # JWT
    "Claims",
    "JWTError",
    "InvalidTokenError",
    "InvalidSignatureError",
    "UnsupportedAlgorithmError",
    "ExpiredSignatureError",
    "ImmatureSignatureError",
    "InvalidAudienceError",
    "InvalidIssuerError",
    "MissingClaimError",
    "encode_jwt",
    "decode_jwt",
    # Reset tokens
    "make_reset_token",
    "check_reset_token",
    "BadResetToken",
    # SSE
    "ServerSentEvent",
    # Testing
    "TestClient",
    "AsyncTestClient",
    # Class-based views
    "View",
    "MethodView",
    # Helpers
    "Aborter",
    "abort",
    "after_this_request",
    "async_send_file",
    "jsonify",
    "make_response",
    "redirect",
    "send_file",
    "send_from_directory",
    "send_from_directory_async",
    "g",
    "current_app",
    "request",
    "session",
    "flash",
    "get_flashed_messages",
    "has_app_context",
    "has_request_context",
    "stream_with_context",
    "url_for",
    # Principal (authenticated identity)
    "Principal",
    "current_principal",
    "set_principal",
    # Templating
    "Jinja2Templates",
    "render_template",
    "render_template_string",
    "stream_template",
    # HTML-safe strings
    "Markup",
    "escape",
    # Encoders
    "jsonable_encoder",
    "register_encoder",
    "unregister_encoder",
    # JSON provider
    "JSONProvider",
    "DefaultJSONProvider",
    "config_orjson_options",
    # Observability
    "RequestMetrics",
    "instrument_access_log",
    "log_requests_as_json",
    "EventLoopWatchdog",
    "HealthPlugin",
    # Signals
    "Signal",
    "Namespace",
    "request_started",
    "request_finished",
    "request_tearing_down",
    "got_request_exception",
    "message_flashed",
    "appcontext_pushed",
    "appcontext_popped",
    "appcontext_tearing_down",
    # Filesystem-safety
    "secure_filename",
    "safe_join",
    "constant_time_compare",
    # Secrets
    "Secret",
    # Signing
    "Signer",
    "BadSignature",
    "BadTimeSignature",
    "BadData",
    # Passwords
    "hash_password",
    "hash_password_async",
    "verify_password",
    "verify_password_async",
    "needs_rehash",
    "verify_and_needs_update",
    "verify_and_needs_update_async",
    "is_strong_password",
    # Status
    "status",
    # Converters
    "Converter",
    "register_converter",
    # Parameter classes
    "Query",
    "Path",
    "Body",
    "Form",
    "File",
    "Header",
    "Cookie",
]


# Names resolved on first access. Each maps to the module that defines it; the
# lookup runs once per name per process, never on a per-request path.
_LAZY_EXPORTS: dict[str, str] = {
    "MCPContext": "veloce.contrib.mcp.context",
}


def __getattr__(name: str) -> Any:
    """Import the module owning a lazily-exported name, then cache it here."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value
