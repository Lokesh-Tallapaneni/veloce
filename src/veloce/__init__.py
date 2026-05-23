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

# Status codes
from veloce import status
from veloce.app import Veloce

# Background tasks
from veloce.background import BackgroundTask, BackgroundTasks
from veloce.blueprints import Blueprint

# Static files
from veloce.contrib.staticfiles import StaticFiles

# Templating — Flask-style top-level shortcuts. The full Jinja2Templates
# class stays under veloce.contrib.templating for callers that want the
# class-based API.
from veloce.contrib.templating import (
    Jinja2Templates,
    render_template,
    render_template_string,
)

# Dependency injection
from veloce.dependency import Depends, Security, SecurityScopes

# Encoders
from veloce.encoders import jsonable_encoder

# Exceptions
from veloce.exceptions import (
    BuildError,
    HTTPException,
    RequestValidationError,
    ValidationError,
    WebSocketDisconnect,
    WebSocketException,
    WebSocketRequestValidationError,
)

# Helpers
from veloce.helpers import (
    abort,
    after_this_request,
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
)

# Data structures
from veloce.http.datastructures import (
    URL,
    AcceptHeader,
    Authorization,
    FormData,
    Headers,
    RangeSpec,
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

# HTML-safe strings
from veloce.markup import Markup, escape

# Middleware
from veloce.middleware import (
    BaseHTTPMiddleware,
    CORSMiddleware,
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
)

# Password hashing helpers
from veloce.passwords import (
    hash_password,
    hash_password_async,
    is_strong_password,
    verify_password,
    verify_password_async,
)
from veloce.routing.params import Body, Cookie, File, Form, Header, Path, Query

# Routing
from veloce.routing.router import Router

# Filesystem-safety helpers
from veloce.safe import constant_time_compare, safe_join, secure_filename

# Security
from veloce.security import (
    APIKeyCookie,
    APIKeyHeader,
    APIKeyQuery,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
    HTTPDigest,
    HTTPDigestCredentials,
    OAuth2AuthorizationCodeBearer,
    OAuth2PasswordBearer,
    OAuth2PasswordRequestForm,
    OAuth2PasswordRequestFormStrict,
    OpenIdConnect,
)

# Sessions
from veloce.sessions import InMemorySessionStore, Session, SessionStore

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
    # Editable install before metadata is materialised, or an
    # unsupported runtime. Fall back to a hand-maintained constant so
    # `veloce.__version__` is never undefined.
    __version__ = "0.1.2"

# some users reach for `APIRouter`; it is the same primitive as
# Veloce's `Blueprint` (a mountable group of routes + hooks).
APIRouter = Blueprint

__all__ = [
    # Core
    "Veloce",
    "Request",
    "Router",
    "Blueprint",
    "APIRouter",
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
    "HTTPException",
    "ValidationError",
    "RequestValidationError",
    "BuildError",
    # Background
    "BackgroundTask",
    "BackgroundTasks",
    # Static
    "StaticFiles",
    # Data structures
    "UploadFile",
    "URL",
    "FormData",
    "Headers",
    "Authorization",
    "AcceptHeader",
    "RangeSpec",
    # Security
    "HTTPBasic",
    "HTTPBasicCredentials",
    "HTTPBearer",
    "HTTPDigest",
    "HTTPDigestCredentials",
    "APIKeyHeader",
    "APIKeyQuery",
    "APIKeyCookie",
    "OAuth2PasswordBearer",
    "OAuth2PasswordRequestForm",
    "OAuth2PasswordRequestFormStrict",
    "OAuth2AuthorizationCodeBearer",
    "OpenIdConnect",
    # SSE
    "ServerSentEvent",
    # Testing
    "TestClient",
    "AsyncTestClient",
    # Class-based views
    "View",
    "MethodView",
    # Helpers
    "abort",
    "after_this_request",
    "jsonify",
    "make_response",
    "redirect",
    "send_file",
    "send_from_directory",
    "g",
    "current_app",
    "request",
    "session",
    "flash",
    "get_flashed_messages",
    "has_app_context",
    "has_request_context",
    "stream_with_context",
    # Templating
    "Jinja2Templates",
    "render_template",
    "render_template_string",
    # HTML-safe strings
    "Markup",
    "escape",
    # Encoders
    "jsonable_encoder",
    # Observability
    "RequestMetrics",
    "EventLoopWatchdog",
    # Filesystem-safety
    "secure_filename",
    "safe_join",
    "constant_time_compare",
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
    "is_strong_password",
    # Status
    "status",
    # Parameter classes
    "Query",
    "Path",
    "Body",
    "Form",
    "File",
    "Header",
    "Cookie",
]
