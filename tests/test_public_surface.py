"""Public import-surface guard.

Freezes what `from veloce import X` and each subpackage gateway expose, so a
refactor that reorganizes internals cannot silently change the public API. Update
a snapshot below ONLY for a deliberate, documented public-API change (with a
CHANGELOG entry) — never to make a refactor pass.
"""

from __future__ import annotations

import importlib

import veloce

VELOCE_ALL = {
    "APIKeyCookie",
    "SessionAuth",
    "login_session",
    "logout_session",
    "APIKeyHeader",
    "APIKeyQuery",
    "APIRouter",
    "Aborter",
    "AcceptHeader",
    "AsyncTestClient",
    "Authorization",
    "BackgroundTask",
    "BackgroundTasks",
    "BadData",
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
    "Converter",
    "Cookie",
    "DefaultJSONProvider",
    "Depends",
    "DuplicateRouteError",
    "EventLoopWatchdog",
    "EventSourceResponse",
    "ExpiredSignatureError",
    "File",
    "FileResponse",
    "FilesKeyError",
    "FixedWindow",
    "Form",
    "FormData",
    "GZipMiddleware",
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
    "ImmatureSignatureError",
    "InMemoryCache",
    "InMemoryRateLimitBackend",
    "InMemorySessionStore",
    "InvalidAudienceError",
    "InvalidIssuerError",
    "InvalidSignatureError",
    "InvalidTokenError",
    "JSONProvider",
    "JSONResponse",
    "JWTError",
    "Jinja2Templates",
    "LoggingMiddleware",
    "MCPContext",
    "Markup",
    "MethodView",
    "Middleware",
    "MissingClaimError",
    "OAuth2AuthorizationCodeBearer",
    "OAuth2PasswordBearer",
    "OAuth2PasswordRequestForm",
    "OAuth2PasswordRequestFormStrict",
    "ORJSONResponse",
    "OpenIdConnect",
    "Path",
    "PlainTextResponse",
    "Plugin",
    "Principal",
    "ProxyFix",
    "Query",
    "RangeSpec",
    "RateLimitBackend",
    "RateLimitMiddleware",
    "RateLimitResult",
    "RateLimitStrategy",
    "RedirectResponse",
    "Request",
    "RequestIDMiddleware",
    "RequestMetrics",
    "RequestValidationError",
    "Response",
    "Router",
    "Secret",
    "Security",
    "SecurityHeadersMiddleware",
    "SecurityScheme",
    "SecurityScopes",
    "ServerSentEvent",
    "ServerSessionMiddleware",
    "Session",
    "SessionMiddleware",
    "SessionStore",
    "SetupError",
    "Signer",
    "SlidingWindow",
    "StaticFiles",
    "StreamingResponse",
    "TestClient",
    "TokenBucket",
    "TrustedHostMiddleware",
    "UJSONResponse",
    "URL",
    "URLRule",
    "UnsupportedAlgorithmError",
    "UploadFile",
    "ValidationError",
    "Veloce",
    "View",
    "WebSocket",
    "WebSocketDisconnect",
    "WebSocketException",
    "WebSocketOriginMiddleware",
    "WebSocketRequestValidationError",
    "abort",
    "after_this_request",
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
    "make_reset_token",
    "make_response",
    "needs_rehash",
    "rate_limit",
    "redirect",
    "register_converter",
    "register_encoder",
    "render_template",
    "render_template_string",
    "request",
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
    },
    "veloce.middleware": {
        "BaseHTTPMiddleware",
        "CORSMiddleware",
        "CSPMiddleware",
        "CSRFMiddleware",
        "ConditionalGetMiddleware",
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


class TestSubPackageImports:
    def test_routing_subpackage(self):
        from veloce.routing import Query, Router

        assert Router is not None
        assert Query is not None

    def test_http_subpackage(self):
        from veloce.http import Request, UploadFile

        assert Request is not None
        assert UploadFile is not None

    def test_middleware_subpackage(self):
        from veloce.middleware import (
            Middleware,
            SessionMiddleware,
        )

        assert Middleware is not None
        assert SessionMiddleware is not None

    def test_security_subpackage(self):
        from veloce.security import (
            HTTPBasic,
            OAuth2PasswordBearer,
        )

        assert HTTPBasic is not None
        assert OAuth2PasswordBearer is not None

    def test_serving_subpackage(self):
        from veloce.serving import HttpProtocol

        assert HttpProtocol is not None

    def test_py_typed_exists(self):
        import os

        import veloce

        pkg_dir = os.path.dirname(veloce.__file__)
        assert os.path.exists(os.path.join(pkg_dir, "py.typed"))
