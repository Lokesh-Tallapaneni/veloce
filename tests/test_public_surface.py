"""Public import-surface guard.

Freezes what `from veloce import X` and each subpackage gateway expose, so a
refactor that reorganizes internals cannot silently change the public API. Update
a snapshot below ONLY for a deliberate, documented public-API change (with a
CHANGELOG entry) — never to make a refactor pass.
"""

from __future__ import annotations

import importlib
import os

import veloce
from veloce.http import Request, UploadFile
from veloce.middleware import Middleware, SessionMiddleware
from veloce.routing import Query, Router
from veloce.security import HTTPBasic, OAuth2PasswordBearer
from veloce.serving import HttpProtocol

VELOCE_ALL = {
    "APIKeyCookie",
    "APIKeyHeader",
    "APIKeyQuery",
    "APIRouter",
    "Aborter",
    "AuditContext",
    "AuditFailed",
    "AcceptHeader",
    "Address",
    "AsyncTestClient",
    "Authorization",
    "BackgroundTask",
    "BackgroundTasks",
    "BadData",
    "BadGateway",
    "BadRequest",
    "BadResetToken",
    "BadSignature",
    "BadTimeSignature",
    "BaseHTTPMiddleware",
    "Blueprint",
    "Body",
    "BuildError",
    "CORSMiddleware",
    "CSPMiddleware",
    "CSRFMiddleware",
    "Cache",
    "Claims",
    "ConditionalGetMiddleware",
    "Config",
    "ConfigurationError",
    "Conflict",
    "Converter",
    "Cookie",
    "Cookies",
    "DefaultJSONProvider",
    "Depends",
    "DuplicateRouteError",
    "EventLoopWatchdog",
    "EventSourceResponse",
    "ExpectationFailed",
    "ExpiredSignatureError",
    "File",
    "FileResponse",
    "Finding",
    "FilesKeyError",
    "FixedWindow",
    "Forbidden",
    "Form",
    "FormData",
    "CompressionMiddleware",
    "GZipMiddleware",
    "GatewayTimeout",
    "Gone",
    "HTMLResponse",
    "HTTPBasic",
    "HTTPBasicCredentials",
    "HTTPBearer",
    "HTTPDigest",
    "HTTPDigestCredentials",
    "HTTPException",
    "HTTPSRedirectMiddleware",
    "Header",
    "Headers",
    "HealthPlugin",
    "ImATeapot",
    "ImmatureSignatureError",
    "InMemoryCache",
    "InMemoryRateLimitBackend",
    "InMemorySessionStore",
    "InternalServerError",
    "InvalidAudienceError",
    "InvalidIssuerError",
    "InvalidSignatureError",
    "InvalidTokenError",
    "JSONProvider",
    "JSONResponse",
    "JWTError",
    "Jinja2Templates",
    "LengthRequired",
    "LoggingMiddleware",
    "MCPContext",
    "Markup",
    "MethodNotAllowed",
    "MethodView",
    "Middleware",
    "MissingClaimError",
    "Namespace",
    "NotAcceptable",
    "NotFound",
    "OAuth2AuthorizationCodeBearer",
    "OAuth2PasswordBearer",
    "OAuth2PasswordRequestForm",
    "OAuth2PasswordRequestFormStrict",
    "ORJSONResponse",
    "OpenIdConnect",
    "Path",
    "PaymentRequired",
    "PlainTextResponse",
    "Plugin",
    "PreconditionFailed",
    "Principal",
    "ProxyAuthenticationRequired",
    "ProxyFix",
    "Query",
    "QueryParams",
    "RangeNotSatisfiable",
    "RangeSpec",
    "RateLimitBackend",
    "RateLimitMiddleware",
    "RateLimitResult",
    "RateLimitStrategy",
    "RedirectResponse",
    "Request",
    "RequestEntityTooLarge",
    "RequestIDMiddleware",
    "RequestMetrics",
    "RequestTimeout",
    "RequestURITooLong",
    "RequestValidationError",
    "Response",
    "Router",
    "Secret",
    "Security",
    "SecurityHeadersMiddleware",
    "SecurityScheme",
    "SecurityScopes",
    "ServerNotImplemented",
    "ServerSentEvent",
    "ServerSessionMiddleware",
    "ServiceUnavailable",
    "Session",
    "SessionAuth",
    "SessionMiddleware",
    "SessionMiddlewareBase",
    "SessionStore",
    "SetupError",
    "Signal",
    "Signer",
    "SlidingWindow",
    "State",
    "StaticFiles",
    "StreamingResponse",
    "TestClient",
    "TestResponse",
    "TokenBucket",
    "TooManyRequests",
    "TrustedHostMiddleware",
    "UJSONResponse",
    "URL",
    "URLRule",
    "Unauthorized",
    "UnprocessableEntity",
    "UnsupportedAlgorithmError",
    "UnsupportedMediaType",
    "UploadFile",
    "ValidationError",
    "Veloce",
    "VeloceDeprecationWarning",
    "VeloceError",
    "View",
    "WebSocket",
    "WebSocketDisconnect",
    "WebSocketException",
    "WebSocketOriginMiddleware",
    "WebSocketRequestValidationError",
    "abort",
    "after_this_request",
    "appcontext_popped",
    "appcontext_pushed",
    "appcontext_tearing_down",
    "async_send_file",
    "cached",
    "check_reset_token",
    "config_orjson_options",
    "constant_time_compare",
    "csp_nonce",
    "current_app",
    "current_principal",
    "decode_jwt",
    "encode_jwt",
    "escape",
    "flash",
    "g",
    "get_flashed_messages",
    "got_request_exception",
    "has_app_context",
    "has_request_context",
    "hash_password",
    "hash_password_async",
    "http_exception_handler",
    "instrument_access_log",
    "is_strong_password",
    "jsonable_encoder",
    "jsonify",
    "log_requests_as_json",
    "login_session",
    "logout_session",
    "make_reset_token",
    "make_response",
    "message_flashed",
    "needs_rehash",
    "rate_limit",
    "redirect",
    "register_converter",
    "register_encoder",
    "unregister_converter",
    "render_template",
    "render_template_string",
    "request",
    "request_finished",
    "request_started",
    "request_tearing_down",
    "request_validation_exception_handler",
    "rotate_csrf_token",
    "safe_join",
    "secure_filename",
    "send_file",
    "send_from_directory",
    "send_from_directory_async",
    "session",
    "set_principal",
    "status",
    "stream_template",
    "stream_with_context",
    "url_for",
    "unregister_encoder",
    "verify_and_needs_update",
    "verify_and_needs_update_async",
    "verify_password",
    "verify_password_async",
}

SUBPACKAGE_ALL = {
    "veloce.http": {
        "AcceptHeader",
        "Address",
        "Authorization",
        "CacheControl",
        "Cookies",
        "FileResponse",
        "FormData",
        "HTMLResponse",
        "HeaderSet",
        "Headers",
        "JSONResponse",
        "ORJSONResponse",
        "PlainTextResponse",
        "QueryParams",
        "RangeSpec",
        "RedirectResponse",
        "Request",
        "Response",
        "State",
        "StreamingResponse",
        "UJSONResponse",
        "URL",
        "UploadFile",
        "header_get",
        "header_key",
        "header_present",
        "header_pop",
        "parse_multipart_form",
    },
    "veloce.routing": {
        "Body",
        "Converter",
        "Cookie",
        "File",
        "Form",
        "Header",
        "Path",
        "Query",
        "RouteInfo",
        "RouteMatch",
        "Router",
        "register_converter",
        "unregister_converter",
    },
    "veloce.middleware": {
        "BaseHTTPMiddleware",
        "CORSMiddleware",
        "CSPMiddleware",
        "CSRFMiddleware",
        "ConditionalGetMiddleware",
        "CompressionMiddleware",
        "GZipMiddleware",
        "HTTPSRedirectMiddleware",
        "LoggingMiddleware",
        "Middleware",
        "ProxyFix",
        "RateLimitMiddleware",
        "RequestIDMiddleware",
        "SecurityHeadersMiddleware",
        "ServerSessionMiddleware",
        "SessionMiddleware",
        "SessionMiddlewareBase",
        "TrustedHostMiddleware",
        "WebSocketOriginMiddleware",
        "csp_nonce",
        "rotate_csrf_token",
    },
    "veloce.security": {
        "APIKeyCookie",
        "SessionAuth",
        "login_session",
        "logout_session",
        "APIKeyHeader",
        "APIKeyQuery",
        "BadResetToken",
        "Claims",
        "ExpiredSignatureError",
        "HTTPBasic",
        "HTTPBasicCredentials",
        "HTTPBearer",
        "HTTPDigest",
        "HTTPDigestCredentials",
        "ImmatureSignatureError",
        "InvalidAudienceError",
        "InvalidIssuerError",
        "InvalidSignatureError",
        "InvalidTokenError",
        "JWTError",
        "MissingClaimError",
        "OAuth2AuthorizationCodeBearer",
        "OAuth2PasswordBearer",
        "OAuth2PasswordRequestForm",
        "OAuth2PasswordRequestFormStrict",
        "OpenIdConnect",
        "SecurityScheme",
        "UnsupportedAlgorithmError",
        "check_reset_token",
        "decode_jwt",
        "encode_jwt",
        "make_reset_token",
    },
    "veloce.serving": {"HttpProtocol"},
    "veloce.contrib": {
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
    },
    "veloce.contrib.mcp.transports": {
        "BidirectionalTransport",
        "MCPRequestError",
        "SessionBackend",
        "SessionRecord",
        "StdioTransport",
        "Transport",
        "register_http_transport",
        "register_sse_transport",
        "serve_stdio",
    },
}


def test_toplevel_surface_unchanged():
    assert set(veloce.__all__) == VELOCE_ALL


def test_every_toplevel_export_is_importable():
    missing = [name for name in veloce.__all__ if not hasattr(veloce, name)]
    assert not missing, f"declared in __all__ but not importable: {missing}"


def test_subpackage_surfaces_unchanged():
    for module_name, expected in SUBPACKAGE_ALL.items():
        module = importlib.import_module(module_name)
        assert set(module.__all__) == expected, module_name


def test_every_subpackage_export_is_importable():
    for module_name in SUBPACKAGE_ALL:
        module = importlib.import_module(module_name)
        missing = [name for name in module.__all__ if not hasattr(module, name)]
        assert not missing, f"{module_name}: not importable: {missing}"


# `veloce.app` is a package whose implementation lives in `veloce.app.core`;
# these names must stay reachable as `veloce.app.X` regardless of how the package
# is split internally (public `Veloce`/`URLRule` plus the private names that
# tests and internal modules reach through the module path).
VELOCE_APP_PATHS = ("Veloce", "URLRule", "_URLMap", "_exc_handler_sig_cache")


def test_veloce_app_paths_resolve():
    app = importlib.import_module("veloce.app")
    missing = [name for name in VELOCE_APP_PATHS if not hasattr(app, name)]
    assert not missing, f"veloce.app.X paths broken by an internal split: {missing}"


def test_routing_subpackage():

    assert Router is not None
    assert Query is not None


def test_http_subpackage():

    assert Request is not None
    assert UploadFile is not None


def test_middleware_subpackage():
    # The import *is* the assertion: this module checks the subpackage
    # publishes these names, so binding them at module top would move
    # the failure to collection and out of the test that reports it.

    assert Middleware is not None
    assert SessionMiddleware is not None


def test_security_subpackage():
    # As above: importing here is what the test asserts.

    assert HTTPBasic is not None
    assert OAuth2PasswordBearer is not None


def test_serving_subpackage():

    assert HttpProtocol is not None


def test_py_typed_exists():
    pkg_dir = os.path.dirname(veloce.__file__)
    assert os.path.exists(os.path.join(pkg_dir, "py.typed"))
