"""Veloce — Ultra-fast async Python web framework.

Veloce is a high-performance asynchronous web framework built on raw asyncio,
httptools, and a native JSON backend. It pairs a small, well-typed API with
predictable
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

# Every public name is resolved on first access instead of at import. A cold
# `import veloce` therefore loads only this table, and the CLI, `veloce.status`
# and test collection stop paying for the whole framework (the router, the
# response classes, pydantic, ...). The imports below exist for type checkers
# and IDEs; `_EXPORTS` is what runs.
if TYPE_CHECKING:  # pragma: no cover
    from veloce._json import JSONBackend, use_json_backend
    from veloce._params import Body, Cookie, File, Form, Header, Path, Query
    from veloce._warnings import VeloceDeprecationWarning
    from veloce.app import Plugin, URLMap, URLRule, Veloce
    from veloce.audit import AuditContext, AuditFailed, Finding, Severity
    from veloce.background import BackgroundTask, BackgroundTasks
    from veloce.blueprints import Blueprint
    from veloce.cache import Cache, InMemoryCache, cached
    from veloce.config import Config
    from veloce.contrib.mcp.context import MCPContext
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
    from veloce.liveserver import LiveServer
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
    from veloce.settings import Settings
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
    "Settings",
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
    "JSONBackend",
    "use_json_backend",
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

# Name -> defining module, or `(module, attribute)` where the public name
# differs from the attribute it resolves to. Resolved once per process:
# `__getattr__` caches the value into `globals()`.
_EXPORTS: dict[str, str | tuple[str, str]] = {
    "JSONBackend": "veloce._json",
    "use_json_backend": "veloce._json",
    "Body": "veloce._params",
    "Cookie": "veloce._params",
    "File": "veloce._params",
    "Form": "veloce._params",
    "Header": "veloce._params",
    "Path": "veloce._params",
    "Query": "veloce._params",
    "VeloceDeprecationWarning": "veloce._warnings",
    "Plugin": "veloce.app",
    "URLMap": "veloce.app",
    "URLRule": "veloce.app",
    "Veloce": "veloce.app",
    "AuditContext": "veloce.audit",
    "AuditFailed": "veloce.audit",
    "Finding": "veloce.audit",
    "Severity": "veloce.audit",
    "BackgroundTask": "veloce.background",
    "BackgroundTasks": "veloce.background",
    "Blueprint": "veloce.blueprints",
    "Cache": "veloce.cache",
    "InMemoryCache": "veloce.cache",
    "cached": "veloce.cache",
    "Config": "veloce.config",
    "MCPContext": "veloce.contrib.mcp.context",
    "StaticFiles": "veloce.contrib.staticfiles",
    "Jinja2Templates": "veloce.contrib.templating",
    "render_template": "veloce.contrib.templating",
    "render_template_string": "veloce.contrib.templating",
    "stream_template": "veloce.contrib.templating",
    "Depends": "veloce.dependency",
    "Security": "veloce.dependency",
    "SecurityScopes": "veloce.dependency",
    "jsonable_encoder": "veloce.encoders",
    "register_encoder": "veloce.encoders",
    "unregister_encoder": "veloce.encoders",
    "BadGateway": "veloce.exceptions",
    "BadRequest": "veloce.exceptions",
    "BuildError": "veloce.exceptions",
    "ConfigurationError": "veloce.exceptions",
    "Conflict": "veloce.exceptions",
    "DuplicateRouteError": "veloce.exceptions",
    "ExpectationFailed": "veloce.exceptions",
    "FilesKeyError": "veloce.exceptions",
    "Forbidden": "veloce.exceptions",
    "GatewayTimeout": "veloce.exceptions",
    "Gone": "veloce.exceptions",
    "HTTPException": "veloce.exceptions",
    "ImATeapot": "veloce.exceptions",
    "InternalServerError": "veloce.exceptions",
    "LengthRequired": "veloce.exceptions",
    "MethodNotAllowed": "veloce.exceptions",
    "NotAcceptable": "veloce.exceptions",
    "NotFound": "veloce.exceptions",
    "PaymentRequired": "veloce.exceptions",
    "PreconditionFailed": "veloce.exceptions",
    "ProxyAuthenticationRequired": "veloce.exceptions",
    "RangeNotSatisfiable": "veloce.exceptions",
    "RequestEntityTooLarge": "veloce.exceptions",
    "RequestTimeout": "veloce.exceptions",
    "RequestURITooLong": "veloce.exceptions",
    "RequestValidationError": "veloce.exceptions",
    "ServerNotImplemented": "veloce.exceptions",
    "ServiceUnavailable": "veloce.exceptions",
    "SetupError": "veloce.exceptions",
    "TooManyRequests": "veloce.exceptions",
    "Unauthorized": "veloce.exceptions",
    "UnprocessableEntity": "veloce.exceptions",
    "UnsupportedMediaType": "veloce.exceptions",
    "ValidationError": "veloce.exceptions",
    "VeloceError": "veloce.exceptions",
    "WebSocketDisconnect": "veloce.exceptions",
    "WebSocketException": "veloce.exceptions",
    "WebSocketRequestValidationError": "veloce.exceptions",
    "http_exception_handler": "veloce.exceptions",
    "request_validation_exception_handler": "veloce.exceptions",
    "HealthPlugin": "veloce.health",
    "Aborter": "veloce.helpers",
    "abort": "veloce.helpers",
    "after_this_request": "veloce.helpers",
    "async_send_file": "veloce.helpers",
    "current_app": "veloce.helpers",
    "flash": "veloce.helpers",
    "g": "veloce.helpers",
    "get_flashed_messages": "veloce.helpers",
    "has_app_context": "veloce.helpers",
    "has_request_context": "veloce.helpers",
    "jsonify": "veloce.helpers",
    "make_response": "veloce.helpers",
    "redirect": "veloce.helpers",
    "request": "veloce.helpers",
    "send_file": "veloce.helpers",
    "send_from_directory": "veloce.helpers",
    "send_from_directory_async": "veloce.helpers",
    "session": "veloce.helpers",
    "stream_with_context": "veloce.helpers",
    "url_for": "veloce.helpers",
    "AcceptHeader": "veloce.http.datastructures",
    "Address": "veloce.http.datastructures",
    "Authorization": "veloce.http.datastructures",
    "Cookies": "veloce.http.datastructures",
    "FormData": "veloce.http.datastructures",
    "Headers": "veloce.http.datastructures",
    "QueryParams": "veloce.http.datastructures",
    "RangeSpec": "veloce.http.datastructures",
    "State": "veloce.http.datastructures",
    "URL": "veloce.http.datastructures",
    "UploadFile": "veloce.http.datastructures",
    "Request": "veloce.http.request",
    "FileResponse": "veloce.http.response",
    "HTMLResponse": "veloce.http.response",
    "JSONResponse": "veloce.http.response",
    "ORJSONResponse": "veloce.http.response",
    "PlainTextResponse": "veloce.http.response",
    "RedirectResponse": "veloce.http.response",
    "Response": "veloce.http.response",
    "StreamingResponse": "veloce.http.response",
    "UJSONResponse": "veloce.http.response",
    "RequestMetrics": "veloce.instrumentation",
    "DefaultJSONProvider": "veloce.json_provider",
    "JSONProvider": "veloce.json_provider",
    "config_orjson_options": "veloce.json_provider",
    "LiveServer": "veloce.liveserver",
    "Markup": "veloce.markup",
    "escape": "veloce.markup",
    "BaseHTTPMiddleware": "veloce.middleware",
    "CORSMiddleware": "veloce.middleware",
    "CSPMiddleware": "veloce.middleware",
    "CSRFMiddleware": "veloce.middleware",
    "CallNext": "veloce.middleware",
    "CompressionMiddleware": "veloce.middleware",
    "ConditionalGetMiddleware": "veloce.middleware",
    "DispatchFunction": "veloce.middleware",
    "GZipMiddleware": "veloce.middleware",
    "HTTPSRedirectMiddleware": "veloce.middleware",
    "LoggingMiddleware": "veloce.middleware",
    "Middleware": "veloce.middleware",
    "ProxyFix": "veloce.middleware",
    "RateLimitMiddleware": "veloce.middleware",
    "RequestIDMiddleware": "veloce.middleware",
    "SecurityHeadersMiddleware": "veloce.middleware",
    "ServerSessionMiddleware": "veloce.middleware",
    "SessionMiddleware": "veloce.middleware",
    "SessionMiddlewareBase": "veloce.middleware",
    "TrustedHostMiddleware": "veloce.middleware",
    "WebSocketOriginMiddleware": "veloce.middleware",
    "csp_nonce": "veloce.middleware",
    "rotate_csrf_token": "veloce.middleware",
    "instrument_access_log": "veloce.observability",
    "log_requests_as_json": "veloce.observability",
    "hash_password": "veloce.passwords",
    "hash_password_async": "veloce.passwords",
    "is_strong_password": "veloce.passwords",
    "needs_rehash": "veloce.passwords",
    "verify_and_needs_update": "veloce.passwords",
    "verify_and_needs_update_async": "veloce.passwords",
    "verify_password": "veloce.passwords",
    "verify_password_async": "veloce.passwords",
    "Principal": "veloce.principal",
    "current_principal": "veloce.principal",
    "set_principal": "veloce.principal",
    "FixedWindow": "veloce.ratelimit",
    "InMemoryRateLimitBackend": "veloce.ratelimit",
    "RateLimitBackend": "veloce.ratelimit",
    "RateLimitResult": "veloce.ratelimit",
    "RateLimitState": "veloce.ratelimit",
    "RateLimitStrategy": "veloce.ratelimit",
    "SlidingWindow": "veloce.ratelimit",
    "TokenBucket": "veloce.ratelimit",
    "rate_limit": "veloce.ratelimit",
    "Converter": "veloce.routing.converters",
    "register_converter": "veloce.routing.converters",
    "unregister_converter": "veloce.routing.converters",
    "APIRouter": ("veloce.routing.router", "Router"),
    "Router": "veloce.routing.router",
    "constant_time_compare": "veloce.safe",
    "safe_join": "veloce.safe",
    "secure_filename": "veloce.safe",
    "Secret": "veloce.secret",
    "APIKeyCookie": "veloce.security",
    "APIKeyHeader": "veloce.security",
    "APIKeyQuery": "veloce.security",
    "BadResetToken": "veloce.security",
    "Claims": "veloce.security",
    "ExpiredSignatureError": "veloce.security",
    "HTTPBasic": "veloce.security",
    "HTTPBasicCredentials": "veloce.security",
    "HTTPBearer": "veloce.security",
    "HTTPDigest": "veloce.security",
    "HTTPDigestCredentials": "veloce.security",
    "ImmatureSignatureError": "veloce.security",
    "InvalidAudienceError": "veloce.security",
    "InvalidIssuerError": "veloce.security",
    "InvalidSignatureError": "veloce.security",
    "InvalidTokenError": "veloce.security",
    "JWTError": "veloce.security",
    "MissingClaimError": "veloce.security",
    "OAuth2AuthorizationCodeBearer": "veloce.security",
    "OAuth2PasswordBearer": "veloce.security",
    "OAuth2PasswordRequestForm": "veloce.security",
    "OAuth2PasswordRequestFormStrict": "veloce.security",
    "OpenIdConnect": "veloce.security",
    "SecurityScheme": "veloce.security",
    "SessionAuth": "veloce.security",
    "UnsupportedAlgorithmError": "veloce.security",
    "check_reset_token": "veloce.security",
    "decode_jwt": "veloce.security",
    "encode_jwt": "veloce.security",
    "login_session": "veloce.security",
    "logout_session": "veloce.security",
    "make_reset_token": "veloce.security",
    "InMemorySessionStore": "veloce.sessions",
    "Session": "veloce.sessions",
    "SessionStore": "veloce.sessions",
    "Namespace": "veloce.signals",
    "Settings": "veloce.settings",
    "Signal": "veloce.signals",
    "SignalResult": "veloce.signals",
    "appcontext_popped": "veloce.signals",
    "appcontext_pushed": "veloce.signals",
    "appcontext_tearing_down": "veloce.signals",
    "got_request_exception": "veloce.signals",
    "message_flashed": "veloce.signals",
    "request_finished": "veloce.signals",
    "request_started": "veloce.signals",
    "request_tearing_down": "veloce.signals",
    "BadData": "veloce.signing",
    "BadSignature": "veloce.signing",
    "BadTimeSignature": "veloce.signing",
    "Signer": "veloce.signing",
    "EventSourceResponse": "veloce.sse",
    "ServerSentEvent": "veloce.sse",
    "AsyncTestClient": "veloce.testclient",
    "TestClient": "veloce.testclient",
    "TestResponse": "veloce.testclient",
    "MethodView": "veloce.views",
    "View": "veloce.views",
    "EventLoopWatchdog": "veloce.watchdog",
    "WebSocket": "veloce.websocket",
    "WebSocketState": "veloce.websocket",
}

#: Names that warn once when first imported, and what to use instead. The
#: warning lives here rather than in a constructor so it costs a response
#: nothing: `__getattr__` caches into `globals()` and runs once per process.
_DEPRECATED_EXPORTS: dict[str, str] = {
    "ORJSONResponse": (
        "`ORJSONResponse` is deprecated and will be removed in 1.0. It aliases "
        "`JSONResponse` and selects no library - encoding follows the configured "
        "backend. Return `JSONResponse` and choose the library with "
        "`use_json_backend(JSONBackend.ORJSON)`, which covers every JSON path "
        "rather than response bodies alone."
    ),
    "UJSONResponse": (
        "`UJSONResponse` is deprecated and will be removed in 1.0. It encodes "
        "with `ujson` directly, bypassing the application's `JSONProvider`, so "
        "an envelope or field convention is dropped on these responses. Return "
        "`JSONResponse` and choose the library with "
        "`use_json_backend(JSONBackend.UJSON)`."
    ),
}


def __getattr__(name: str) -> Any:
    """Import the module owning a lazily-exported name, then cache it here."""
    if name == "__version__":
        # Reading the installed package metadata walks the distribution's files,
        # which every CLI invocation and cold start would otherwise pay for.
        from veloce._version import resolve_version

        globals()["__version__"] = value = resolve_version()
        return value
    import importlib

    target = _EXPORTS.get(name)
    if target is None:
        # Not an export: a submodule reached as an attribute (`veloce.http`)
        # still resolves, as it did when every subpackage was imported eagerly.
        if name.startswith("__"):
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        qualified = f"{__name__}.{name}"
        try:
            submodule = importlib.import_module(qualified)
        except ModuleNotFoundError as exc:
            if exc.name != qualified:
                raise
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
        globals()[name] = submodule
        return submodule
    message = _DEPRECATED_EXPORTS.get(name)
    if message is not None:
        import warnings

        from veloce._warnings import VeloceDeprecationWarning

        warnings.warn(message, VeloceDeprecationWarning, stacklevel=2)
    module_name, attr = (target, name) if isinstance(target, str) else target
    value = getattr(importlib.import_module(module_name), attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
