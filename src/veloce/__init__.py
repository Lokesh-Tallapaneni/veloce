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

from typing import TYPE_CHECKING, Any

from veloce import status
from veloce._params import Body, Cookie, File, Form, Header, Path, Query
from veloce._warnings import VeloceDeprecationWarning
from veloce.app import Plugin, URLMap, URLRule, Veloce
from veloce.audit import AuditContext, AuditFailed, Finding, Severity
from veloce.background import BackgroundTask, BackgroundTasks
from veloce.blueprints import Blueprint
from veloce.cache import Cache, InMemoryCache, cached
from veloce.config import Config
from veloce.contrib.staticfiles import StaticFiles

# Top-level shortcuts; the `Jinja2Templates` class-based API stays under
# `veloce.contrib.templating`.
from veloce.contrib.templating import (
    Jinja2Templates,
    render_template,
    render_template_string,
    stream_template,
)
from veloce.dependency import Depends, Security, SecurityScopes
from veloce.encoders import jsonable_encoder, register_encoder, unregister_encoder
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

# The Prometheus and OpenTelemetry instrumentors stay under veloce.metrics /
# veloce.otel: importing them costs ~72 ms and ~32 ms at `import veloce`.
from veloce.health import HealthPlugin
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
from veloce.json_provider import (
    DefaultJSONProvider,
    JSONProvider,
    config_orjson_options,
)
from veloce.markup import Markup, escape
from veloce.middleware import (
    BaseHTTPMiddleware,
    CallNext,
    CompressionMiddleware,
    ConditionalGetMiddleware,
    CORSMiddleware,
    CSPMiddleware,
    CSRFMiddleware,
    DispatchFunction,
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
    SessionMiddlewareBase,
    TrustedHostMiddleware,
    WebSocketOriginMiddleware,
    csp_nonce,
    rotate_csrf_token,
)
from veloce.observability import instrument_access_log, log_requests_as_json
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
from veloce.principal import Principal, current_principal, set_principal
from veloce.ratelimit import (
    FixedWindow,
    InMemoryRateLimitBackend,
    RateLimitBackend,
    RateLimitResult,
    RateLimitState,
    RateLimitStrategy,
    SlidingWindow,
    TokenBucket,
    rate_limit,
)
from veloce.routing.converters import Converter, register_converter, unregister_converter
from veloce.routing.router import Router
from veloce.safe import constant_time_compare, safe_join, secure_filename
from veloce.secret import Secret
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
from veloce.sessions import InMemorySessionStore, Session, SessionStore
from veloce.signals import (
    Namespace,
    Signal,
    SignalResult,
    appcontext_popped,
    appcontext_pushed,
    appcontext_tearing_down,
    got_request_exception,
    message_flashed,
    request_finished,
    request_started,
    request_tearing_down,
)
from veloce.signing import BadData, BadSignature, BadTimeSignature, Signer
from veloce.sse import EventSourceResponse, ServerSentEvent
from veloce.testclient import AsyncTestClient, TestClient, TestResponse
from veloce.views import MethodView, View
from veloce.watchdog import EventLoopWatchdog
from veloce.websocket import WebSocket, WebSocketState

# Resolved on first access: reaching `MCPContext` eagerly would initialise the
# whole MCP subpackage at every `import veloce`, for one re-exported name.
if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.context import MCPContext
    from veloce.liveserver import LiveServer

# `APIRouter` aliases `Router` (`prefix=`, `tags=`, `dependencies=`,
# `responses=`); a route group needing a name or hooks uses `Blueprint`.
APIRouter = Router

__all__ = [
    # Core
    "Veloce",
    "Plugin",
    "Request",
    "Router",
    "Blueprint",
    "APIRouter",
    "URLMap",
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
    "CallNext",
    "CORSMiddleware",
    "CSRFMiddleware",
    "DispatchFunction",
    "ConditionalGetMiddleware",
    "CompressionMiddleware",
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
    "RateLimitState",
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
    "SessionMiddlewareBase",
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
    "WebSocketState",
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
    "TestResponse",
    "LiveServer",
    # Class-based views
    "View",
    "MethodView",
    # Helpers
    "Aborter",
    "AuditContext",
    "AuditFailed",
    "Finding",
    "Severity",
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
    "SignalResult",
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
    "unregister_converter",
    # Parameter classes
    "Query",
    "Path",
    "Body",
    "Form",
    "File",
    "Header",
    "Cookie",
]


# Each name maps to the module that defines it; resolved once per process.
_LAZY_EXPORTS: dict[str, str] = {
    "MCPContext": "veloce.contrib.mcp.context",
    "LiveServer": "veloce.liveserver",
}


def __getattr__(name: str) -> Any:
    """Import the module owning a lazily-exported name, then cache it here."""
    if name == "__version__":
        # Reading the installed package metadata walks the distribution's files,
        # which every CLI invocation and cold start would otherwise pay for.
        from veloce._version import resolve_version

        globals()["__version__"] = value = resolve_version()
        return value
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value
