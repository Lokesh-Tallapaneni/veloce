"""Veloce application - the main entry point."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import signal
import time
import traceback
import warnings
import weakref
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, get_args, get_origin

from pydantic import BaseModel as _PydanticBaseModel

from veloce import status
from veloce._constants import (
    HEADER_ACCEPT,
    HEADER_ALLOW,
    HEADER_HOST,
    MIME_TEXT_HTML,
    MIME_TEXT_HTML_UTF8,
    MIME_TEXT_PLAIN,
    MIME_TEXT_PLAIN_UTF8,
    MSG_INTERNAL_SERVER_ERROR,
    MSG_LABEL_HEADER_NAME,
    MSG_LABEL_SET_COOKIE_VALUE,
    MSG_METHOD_NOT_ALLOWED,
    MSG_NOT_FOUND,
    MSG_REQUEST_BODY_EXCEEDS_MAX,
)
from veloce._internal import (
    MIME_HTML,
    MIME_JSON,
    MIME_OCTET,
    _coerce_bool,
    _encode_header_value,
    _extract_host,
    _is_async_callable,
    _reject_header_crlf,
)
from veloce._protocol_constants import (
    ASGI_EVENT_HTTP_RESPONSE_BODY,
    ASGI_EVENT_HTTP_RESPONSE_START,
    ASGI_EVENT_LIFESPAN_SHUTDOWN,
    ASGI_EVENT_LIFESPAN_SHUTDOWN_COMPLETE,
    ASGI_EVENT_LIFESPAN_STARTUP,
    ASGI_EVENT_LIFESPAN_STARTUP_COMPLETE,
    ASGI_EVENT_LIFESPAN_STARTUP_FAILED,
    ASGI_EVENT_WS_CLOSE,
    ASGI_EVENT_WS_CONNECT,
    ASGI_SCOPE_HTTP,
    ASGI_SCOPE_LIFESPAN,
    ASGI_SCOPE_WEBSOCKET,
    HTTP_METHOD_GET,
    HTTP_METHOD_HEAD,
    HTTP_METHOD_OPTIONS,
    LIFECYCLE_SHUTDOWN,
    LIFECYCLE_STARTUP,
    RAW_HEADER_CONTENT_LENGTH,
    RAW_HEADER_CONTENT_TYPE,
    RAW_HEADER_SET_COOKIE,
    ROUTE_METHOD_WEBSOCKET,
    TRACE_HEADER_TRACEPARENT,
    TRACE_HEADER_TRACESTATE,
    URL_SCHEME_HTTP,
    URL_SCHEME_HTTPS,
)
from veloce.blueprints import _endpoint_blueprint
from veloce.contrib.staticfiles import StaticFiles
from veloce.debug import render_traceback_html
from veloce.dependency import DependencyResolver, Depends
from veloce.exceptions import (
    HTTPException,
    RequestValidationError,
    WebSocketException,
    WebSocketRequestValidationError,
)
from veloce.helpers import _current_app_var, _current_request_var, _RequestGlobals, g
from veloce.http.datastructures import State
from veloce.http.request import Request
from veloce.http.response import (
    JSONResponse,
    RedirectResponse,
    Response,
)
from veloce.instrumentation import RequestMetrics
from veloce.middleware import BaseHTTPMiddleware, Middleware
from veloce.routing.router import Router
from veloce.sessions import Session
from veloce.signals import (
    appcontext_popped,
    appcontext_pushed,
    appcontext_tearing_down,
    got_request_exception,
    request_finished,
    request_started,
    request_tearing_down,
)
from veloce.websocket import WebSocket

if TYPE_CHECKING:  # pragma: no cover
    import ssl


# Cache of `(wants_request, wants_exc)` flags per exception handler - the
# `inspect.signature` walk inside `_call_exc_handler` repeats on every
# raised exception otherwise. WeakKey so handler GC reclaims the entry.
_exc_handler_sig_cache: weakref.WeakKeyDictionary[Callable[..., Any], tuple[bool, bool]] = (
    weakref.WeakKeyDictionary()
)

# Sentinel for cache misses where `None` is itself a valid cache hit
# (e.g. "no exception handler matched this type"). Plain `cache.get(k)`
# would re-walk the MRO every time for an unhandled exception type.
_MISSING: Any = object()

# Pre-encoded ASCII bytes for the content-type strings the built-in
# response classes emit. Hit at ASGI emit time before the per-request
# `_reject_header_crlf(...).encode()` round-trip; values here are
# trusted (they originate from response.py class definitions) so the
# CRLF/NUL check is skipped on cache hit. Mutation of the cached
# strings is impossible - str is immutable - so a handler-side write
# like `response.content_type = "text/csv"` falls through to the
# uncached path and is validated as before.
_CT_BYTES_CACHE: dict[str, bytes] = {
    MIME_JSON: MIME_JSON.encode("ascii"),
    MIME_HTML: MIME_HTML.encode("ascii"),
    MIME_TEXT_PLAIN_UTF8: MIME_TEXT_PLAIN_UTF8.encode("ascii"),
    MIME_OCTET: MIME_OCTET.encode("ascii"),
}

# Pre-encoded ASCII bytes for small content-length values. Body sizes
# below 2048 cover the entire json-hello / path-param hot path and the
# vast majority of typical JSON API responses; larger payloads fall
# through to the per-request `str(n).encode()` allocation.
_CL_BYTES_SMALL: tuple[bytes, ...] = tuple(str(i).encode("ascii") for i in range(2048))


def _prefers_html(request: Request) -> bool:
    """Whether the client prefers an HTML response over plain text.

    Used by the debug traceback page: a browser (`Accept: text/html`) gets the
    rich HTML view, while curl / CLI / programmatic clients (`*/*`, no Accept,
    or an explicit text/plain preference) keep the plain-text traceback. A
    missing Accept header is treated as "no HTML preference" -> plain text,
    preserving the pre-existing debug Content-Type for non-browser clients.
    """
    accept = request.headers.get(HEADER_ACCEPT)
    if not accept:
        return False
    return request.accept_mimetypes.best_match([MIME_TEXT_PLAIN, MIME_TEXT_HTML]) == MIME_TEXT_HTML


def _trace_carrier(request: Request) -> dict[str, str] | None:
    """Inbound W3C trace headers as a carrier dict, or `None` if absent.

    Only `traceparent` / `tracestate` are copied - the dimensions a tracing
    bridge needs to continue a distributed trace - keeping the framework core
    free of any OpenTelemetry dependency. Returns `None` (not an empty dict)
    when no `traceparent` is present so the bridge can cheaply skip extraction.
    """
    traceparent = request.headers.get(TRACE_HEADER_TRACEPARENT)
    if traceparent is None:
        return None
    carrier = {TRACE_HEADER_TRACEPARENT: traceparent}
    tracestate = request.headers.get(TRACE_HEADER_TRACESTATE)
    if tracestate is not None:
        carrier[TRACE_HEADER_TRACESTATE] = tracestate
    return carrier


class _LifespanManager:
    """Async context manager driving the app's lifespan cycle.

    `async with app.lifespan_context(): ...` runs startup on entry and
    shutdown on exit. Re-entrant guard: a second `__aenter__` without
    an intervening `__aexit__` raises, since lifespan is once-per-app.
    """

    __slots__ = ("_app", "_entered")

    def __init__(self, app: Veloce) -> None:
        self._app = app
        self._entered = False

    async def __aenter__(self) -> Veloce:
        if self._entered:
            raise RuntimeError("lifespan_context already entered")
        self._entered = True
        await self._app._run_lifecycle(LIFECYCLE_STARTUP)
        return self._app

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._app._run_lifecycle(LIFECYCLE_SHUTDOWN)
        self._entered = False


class _AppContext:
    """Outside-request binding for `current_app` and `g`.

    Implemented as a re-entrant context manager: nested
    `with app.app_context(): ...` blocks restore the previous binding on
    exit (via the `ContextVar` token returned by `set()`), so two apps
    in one process don't bleed into each other.
    """

    __slots__ = ("_app", "_app_token", "_g_token")

    def __init__(self, app: Veloce) -> None:
        self._app = app
        self._app_token: Any = None
        self._g_token: Any = None

    def __enter__(self) -> Veloce:
        self._app_token = _current_app_var.set(self._app)
        # Fresh `g` store - each app_context block gets its own.
        self._g_token = _RequestGlobals._ctx_var.set({})
        appcontext_pushed.send(self._app)
        return self._app

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        appcontext_tearing_down.send(self._app, exc=exc)
        if self._app_token is not None:
            _current_app_var.reset(self._app_token)
        if self._g_token is not None:
            _RequestGlobals._ctx_var.reset(self._g_token)
        appcontext_popped.send(self._app)


class _TestRequestContext:
    """Synthesises a request for tests/scripts without running dispatch.

    Inside the block: `current_app`, `g`, and `request._state` resolve.
    Outside: the bindings are unwound. No middleware, no DI, no handler
    - that's what `TestClient` is for. This is for unit tests that just
    need `current_app.config[...]` or `g.foo = ...` to work in isolation.
    """

    __slots__ = ("_app_ctx", "_request", "_request_token")

    def __init__(
        self,
        app: Veloce,
        path: str,
        method: str,
        headers: dict[str, str],
        query_string: str,
        body: bytes,
    ) -> None:
        self._app_ctx = _AppContext(app)
        self._request = Request(
            method=method,
            path=path,
            query_string=query_string,
            headers=headers,
            body=body,
        )
        self._request.app = app
        self._request_token: Any = None

    def __enter__(self) -> Request:
        self._app_ctx.__enter__()
        # Stash the synthetic request on a contextvar so user code can
        # read it via the same `current_request`-style helpers used at
        # dispatch time.
        # Provide an in-memory `Session` so helpers that read the
        # request's session (`flash`, `get_flashed_messages`,
        # `session` proxy) work inside the block without requiring
        # the caller to also install `SessionMiddleware`. Production
        # dispatch installs one via the middleware; the context just
        # mirrors that surface.
        if "session" not in self._request._state:
            self._request._state["session"] = Session()
        self._request_token = _current_request_var.set(self._request)
        return self._request

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._request_token is not None:
            _current_request_var.reset(self._request_token)
        self._app_ctx.__exit__(exc_type, exc, tb)


class URLRule:
    """A single registered URL rule view object.

    Iterable over its fields as `(rule, methods, endpoint)` so callers
    that just want tuple-unpack semantics work; full attribute access
    gives `rule`, `methods`, `endpoint`, `defaults`, `host`, etc. for
    introspection.
    """

    __slots__ = ("rule", "methods", "endpoint")

    def __init__(self, rule: str, methods: list[str], endpoint: str) -> None:
        self.rule = rule
        self.methods = methods
        self.endpoint = endpoint

    def __iter__(self) -> Any:
        return iter((self.rule, self.methods, self.endpoint))

    def __repr__(self) -> str:
        return f"<URLRule {self.endpoint}: {','.join(self.methods)} {self.rule}>"


class _URLMap:
    """Veloce's read-only `Map`-style route-table wrapper.

    Iterating yields `URLRule` objects in registration order (grouped
    by `(path, name)` so each unique route is one rule even when
    several HTTP methods share it). `len()` counts unique rules.
    Lookup by endpoint name returns the list of matching rules.
    """

    __slots__ = ("_app", "_cached")

    def __init__(self, app: Veloce) -> None:
        self._app = app
        self._cached: list[URLRule] | None = None

    def _build(self) -> list[URLRule]:
        # Collect every (method, path, info) tuple, then group by
        # (path, endpoint-name) so a route registered for both GET and
        # POST shows up as a single rule. Result is cached on the
        # `_URLMap` instance; the app drops the whole instance via
        # `_invalidate_route_caches()` on any route mutation, so the
        # cache cannot go stale.
        cached = self._cached
        if cached is not None:
            return cached
        groups: dict[tuple[str, str], URLRule] = {}
        for method, path, info in self._app._collect_all_routes():
            key = (path, info.name)
            existing = groups.get(key)
            if existing is None:
                groups[key] = URLRule(rule=path, methods=[method], endpoint=info.name)
            else:
                existing.methods.append(method)
        result = list(groups.values())
        self._cached = result
        return result

    def __iter__(self) -> Any:
        return iter(self._build())

    def __len__(self) -> int:
        return len(self._build())

    def __getitem__(self, endpoint: str) -> list[URLRule]:
        return [r for r in self._build() if r.endpoint == endpoint]

    def __repr__(self) -> str:
        rules = self._build()
        return f"<URLMap with {len(rules)} rule{'s' if len(rules) != 1 else ''}>"


class Veloce(Router):
    """Ultra-fast async web framework.

    Usage:
        app = Veloce()

        @app.get("/")
        async def index(request: Request):
            return {"message": "Hello, World!"}

        app.run()
    """

    def __init__(
        self,
        title: str = "Veloce",
        version: str = "0.1.0",
        description: str = "",
        summary: str | None = None,
        debug: bool = False,
        prefix: str = "",
        docs_url: str | None = "/docs",
        redoc_url: str | None = "/redoc",
        openapi_url: str | None = "/openapi.json",
        lifespan: Callable | None = None,
        redirect_slashes: bool = True,
        root_path: str = "",
        openapi_tags: list[dict[str, Any]] | None = None,
        openapi_external_docs: dict[str, Any] | None = None,
        servers: list[dict[str, Any]] | None = None,
        license_info: dict[str, str] | None = None,
        contact: dict[str, str] | None = None,
        terms_of_service: str | None = None,
        swagger_ui_parameters: dict[str, Any] | None = None,
        swagger_ui_init_oauth: dict[str, Any] | None = None,
        default_response_class: Any = None,
        dependencies: list[Any] | None = None,
        responses: dict[int, dict[str, Any]] | None = None,
        exception_handlers: dict[Any, Callable] | None = None,
        middleware: list[Any] | None = None,
        import_name: str | None = None,
        template_folder: str | None = None,
        instance_path: str | None = None,
        **extra: Any,
    ) -> None:
        # App-level `dependencies` / `responses` - applied
        # to every route registered on the app (per-route entries are
        # appended / overlaid on top).
        super().__init__(
            prefix=prefix,
            default_response_class=default_response_class,
            dependencies=dependencies,
            responses=responses,
        )
        # arbitrary `**extra` ctor kwargs are stashed on
        # `app.extra` for extensions / OpenAPI customisation to read.
        self.extra: dict[str, Any] = dict(extra)
        # instance folder - explicit override, else computed from
        # `package_root` on first `instance_path` access.
        self._instance_path = instance_path
        # `import_name` - defaults to the caller's module so
        # `Veloce(__name__)` works. Used to compute `root_path` (the
        # package directory) for template / static-file resolution.
        if import_name is None:
            import sys

            frame = sys._getframe(1)
            import_name = frame.f_globals.get("__name__", "veloce.app")
        self.import_name = import_name
        self.title = title
        self.version = version
        self.description = description
        # OpenAPI 3.1 Sec. 4.8.2 `info.summary` - a short one-line summary
        # of the API, distinct from the longer `description`.
        self.summary = summary
        self._docs_url = docs_url
        self._redoc_url = redoc_url
        self._openapi_url = openapi_url
        self._openapi_setup = False
        self.openapi_schema: dict[str, Any] | None = None
        self.redirect_slashes = redirect_slashes
        self.root_path = root_path
        self.openapi_tags = openapi_tags
        self.openapi_external_docs = openapi_external_docs
        self.servers = servers
        self.license_info = license_info
        self.contact = contact
        self.terms_of_service = terms_of_service
        self.swagger_ui_parameters = swagger_ui_parameters
        self.swagger_ui_init_oauth = swagger_ui_init_oauth

        from veloce.config import Config

        self.state: State = State()
        # Configuration. `Config` is a dict subclass with
        # loader methods (from_object, from_pyfile, from_mapping, ...).
        # Seeded with the documented default keys so `app.config[k]`
        # returns a value rather than raising `KeyError`.
        self.config: Config = Config(Config.default_config())
        # `debug` is a property bound to `config["DEBUG"]` (below), so seed the
        # config key from the constructor arg - this is the single source of
        # truth, keeping `app.debug` and `config["DEBUG"]` from drifting apart.
        self.config["DEBUG"] = debug
        self.secret_key: str | None = None  # Secret key
        self.extensions: dict[str, Any] = {}  # Extensions registry
        self._lifespan = lifespan
        self._lifespan_cm: Any = None

        # Set up logger: the logger name is the
        # `import_name` (already resolved to the caller's module above
        # when not passed explicitly).
        import logging

        self.logger = logging.getLogger(self.import_name)

        self._middlewares: list[Middleware] = []
        # Standard ASGI middleware - `(class, options)` pairs. Each wraps the
        # whole ASGI application (instantiated as `cls(app, **options)`) and
        # is assembled lazily into `_asgi_stack` on the first request.
        self._asgi_middleware: list[tuple[Any, dict[str, Any]]] = []
        self._asgi_stack: Callable | None = None
        # Observability instrumentation hooks - each is invoked once per
        # finished HTTP request with a `RequestMetrics` record. Empty by
        # default, so an un-instrumented app pays nothing.
        self._instrumentation: list[Callable] = []
        # Dev-mode event-loop blocking watchdog - armed during startup only
        # when the `EVENT_LOOP_WATCHDOG` config key is set, so it is `None`
        # (and free) for every other app.
        self._watchdog: Any = None
        self._exception_handlers: dict[type, Callable] = {}
        self._status_handlers: dict[int, Callable] = {}
        # Route-introspection caches - rebuilt lazily on next access after
        # a mutation. Invalidated through `_invalidate_route_caches()`,
        # which fires from `add_route` / `include_router` (the two
        # entry-points every higher-level registration ultimately funnels
        # through, including `register_blueprint` and `add_url_rule`).
        self._cached_routes: list[dict[str, Any]] | None = None
        self._cached_view_functions: dict[str, Callable] | None = None
        self._cached_url_map: _URLMap | None = None
        # Cached `_find_exception_handler` MRO walks; invalidated on
        # any `register_error_handler` call. The cache assumes the
        # exception-type space is bounded - typical applications raise
        # a fixed set of exception classes, so it never grows beyond a
        # few dozen entries. An app that synthesises new exception
        # classes per request would grow this unboundedly; not a target
        # workload.
        self._exc_handler_cache: dict[type, Callable | None] = {}
        # `exception_handlers=` ctor mapping - keys are
        # exception classes or integer status codes.
        for _key, _handler in (exception_handlers or {}).items():
            self.add_exception_handler(_key, _handler)
        # ASGI shape `middleware=` ctor list - each entry is
        # a middleware instance applied in the given order.
        for _mw in middleware or []:
            self.add_middleware(_mw)
        self._on_startup: list[Callable] = []
        self._on_shutdown: list[Callable] = []
        self._static_handlers: list[StaticFiles] = []
        self._dependency_overrides: dict[Callable, Callable] = {}
        # Cross-request cache of `(sub_plan, is_coro, is_gen, is_async_gen)`
        # for overridden dependencies. Hoisted to the app so each request's
        # fresh DependencyResolver doesn't pay the build + triple-probe cost.
        # WeakKeyDictionary so a transient override target (a per-test
        # lambda, a hot-reloaded factory) does not pin its plan for the
        # process lifetime - strong-keyed callable caches become leaks
        # under test-suite churn.
        self._override_subplans: weakref.WeakKeyDictionary[Callable, Any] = (
            weakref.WeakKeyDictionary()
        )
        self._before_request_hooks: list[Callable] = []
        self._before_first_request_hooks: list[Callable] = []
        # Single-fire guard: lock prevents concurrent first requests from
        # both seeing `_first_request_fired = False` and running hooks twice.
        # The lock itself is lazy-allocated on first use so it binds to the
        # currently-running event loop, not to whatever loop happens to be
        # current at app-construction time (which is typically no loop at
        # all when Veloce() is instantiated at module scope, and a
        # different loop when TestClient spins one up later).
        self._first_request_fired = False
        self._first_request_lock: asyncio.Lock | None = None
        self._after_request_hooks: list[Callable] = []
        self._teardown_request_hooks: list[Callable] = []
        # Blueprint hooks bucketed by blueprint name. Dispatch only walks
        # the bucket whose name matches the matched route's `endpoint`
        # prefix, avoiding the O(B*H) per-request no-op gate iteration
        # the flattened-with-startswith-gate approach used to incur.
        self._bp_before_hooks: dict[str, list[Callable]] = {}
        self._bp_after_hooks: dict[str, list[Callable]] = {}
        self._bp_teardown_hooks: dict[str, list[Callable]] = {}
        self._teardown_appcontext_hooks: list[Callable] = []
        self._context_processors: list[Callable] = []
        # `(prefix, prefix + "/", sub_app)` - the second slot is the
        # boundary string the dispatcher compares against the request path,
        # precomputed once so the per-request loop avoids re-allocating it.
        self._mounted_apps: list[tuple[str, str, Any]] = []
        # Same shape for ASGI-layer mounts dispatched with the raw scope.
        self._asgi_mounts: list[tuple[str, str, Any]] = []
        self._http_middleware_funcs: list[Callable] = []  # @app.middleware("http") funcs
        # Jinja2 helper registrations - applied to the env on each render.
        self._template_filters: list[tuple[str, Callable]] = []
        self._template_globals: list[tuple[str, Callable]] = []
        self._template_tests: list[tuple[str, Callable]] = []
        # URL processors: preprocessor runs after route match and
        # can mutate path_params (e.g. pop a lang segment into g); url_defaults
        # runs inside url_for/url_path_for and can inject default kwargs.
        self._url_value_preprocessors: list[Callable] = []
        self._url_default_funcs: list[Callable] = []
        # `url_build_error_handlers` - list of `(error, endpoint, values)`
        # callbacks consulted when `url_for` can't build a URL.
        self.url_build_error_handlers: list[Callable] = []
        # `app.blueprints` view + `iter_blueprints()` iterator -
        # name -> Blueprint of every successfully registered blueprint.
        self._blueprints_map: dict[str, Any] = {}
        # `@app.shell_context_processor` registry - each function
        # returns a dict that's merged into `veloce shell`'s namespace.
        self._shell_context_processors: list[Callable] = []
        # Lazily-built `click.Group` for app-defined CLI commands. Built
        # on first `app.cli` access so `click` isn't a hard import.
        self._cli_group: Any = None
        # `app.webhooks` - an APIRouter whose routes are pure
        # documentation: registered for the OpenAPI 3.1 `webhooks`
        # section, never dispatched.
        from veloce.blueprints import Blueprint

        self.webhooks = Blueprint("webhooks")
        # JSON provider - the. Class attribute is overridable;
        # instance is built lazily on first `app.json` access.
        from veloce.json_provider import DefaultJSONProvider

        self.json_provider_class: Any = DefaultJSONProvider
        self._json_provider: Any = None
        # Callable `Aborter`. Lazily built on first `app.aborter`
        # access so subclasses can override before use without paying
        # construction cost for apps that don't touch it.
        self._aborter: Any = None
        # Static-folder attributes - `static_folder` is
        # resolved relative to `package_root` if not absolute. Mounting
        # a `StaticFiles` handler at `static_url_path` is opt-in via
        # `app.static(prefix=app.static_url_path, directory=app.static_folder)`.
        self.static_folder: str = "static"
        self.static_url_path: str = "/static"
        # `template_folder`: when set, build a Jinja2Templates
        # and bind it on `app._templates` so `render_template(name, ...)`
        # works without manual wiring. Relative paths resolve under
        # `package_root` (same convention as static_folder).
        self.template_folder: str | None = template_folder
        self._templates: Any = None
        if template_folder is not None:
            import os

            from veloce.contrib.templating import Jinja2Templates

            tdir = template_folder
            if not os.path.isabs(tdir):
                tdir = os.path.join(self.package_root, tdir)
            self._templates = Jinja2Templates(directory=tdir)

    # -- Middleware ------------------------------------------------

    # -- Properties ---------------------------------------------

    @property
    def debug(self) -> bool:
        """Whether debug mode is enabled; bound to `config['DEBUG']`.

        Interprets a dotenv-style string (`DEBUG=false`) correctly rather than
        treating any non-empty string as truthy.
        """
        return _coerce_bool(self.config.get("DEBUG", False))

    @debug.setter
    def debug(self, value: bool) -> None:
        # Coerce the same way the getter does, so `app.debug = "false"` (a
        # string from an env source) stores False rather than a truthy string.
        self.config["DEBUG"] = _coerce_bool(value)

    @property
    def url_map(self) -> _URLMap:
        """Read-only mapping of registered URL rules.

        Iterating it yields `URLRule` objects (rule, methods, endpoint).
        Subscript by endpoint name (`app.url_map["users.detail"]`) returns
        a list of rules registered under that endpoint. Length is the
        total registered route count.

        This is the introspection-friendly view of `Veloce.routes`;
        callers who just want the dict-list keep using `app.routes`.
        """
        cached = self._cached_url_map
        if cached is None:
            cached = _URLMap(self)
            self._cached_url_map = cached
        return cached

    @property
    def routes(self) -> list[dict[str, Any]]:
        """List all registered routes."""
        cached = self._cached_routes
        if cached is not None:
            return cached
        result = []
        for method, path, info in self._collect_all_routes():
            result.append(
                {
                    "path": path,
                    "method": method,
                    "name": info.name,
                    "summary": info.summary,
                    "tags": info.tags,
                    "deprecated": info.deprecated,
                }
            )
        self._cached_routes = result
        return result

    def _invalidate_route_caches(self) -> None:
        """Drop all cached views of the route table.

        Called from every route-mutation entry-point (`add_route`,
        `include_router`); `register_blueprint` and `add_url_rule`
        funnel through `add_route` so they are covered transitively.
        Also resets the `_URLMap` instance cache so its own built-list
        cache is rebuilt on next access.
        """
        self._cached_routes = None
        self._cached_view_functions = None
        self._cached_url_map = None

    def add_route(self, *args: Any, **kwargs: Any) -> None:
        super().add_route(*args, **kwargs)
        self._invalidate_route_caches()

    def include_router(self, router: Any, prefix: str = "", url_prefix: str | None = None) -> None:
        """Mount a sub-router `include_router`.

        Accepts either a `Blueprint` (delegates to `register_blueprint`,
        honouring its hooks / error handlers / url processors) or a
        plain `Router` (delegates to `Router.include_router`). The
        `prefix` and `url_prefix` are interchangeable; both spellings
        spells it `prefix`, Veloce spells it `url_prefix`.
        """
        from veloce.blueprints import Blueprint

        effective = url_prefix if url_prefix is not None else (prefix or None)
        if isinstance(router, Blueprint):
            self.register_blueprint(router, url_prefix=effective)
        else:
            Router.include_router(self, router, prefix=effective or "")
            self._invalidate_route_caches()

    # -- Middleware ------------------------------------------------

    def add_middleware(self, middleware: Any, **options: Any) -> None:
        """Add middleware to the pipeline.

        Call forms:

        - `add_middleware(VeloceMiddlewareClass, **options)` - a class
          subclassing `Middleware` is instantiated with `**options` and
          appended to the request/response pipeline.
        - `add_middleware(instance)` - append an already-built `Middleware`
          instance directly.
        - `add_middleware(ASGIMiddlewareClass, **options)` - a class that
          is *not* a `Middleware` subclass is treated as a standard ASGI
          middleware: it wraps the whole application and is instantiated
          as `ASGIMiddlewareClass(app, **options)` when the ASGI stack is
          assembled. This is what lets third-party ASGI middleware
          (observability, tracing, profiling, ...) plug in. Middleware
          added first is the outermost wrapper.
        """
        if isinstance(middleware, type):
            if issubclass(middleware, Middleware):
                self._middlewares.append(middleware(**options))
            elif issubclass(middleware, BaseHTTPMiddleware):
                # `BaseHTTPMiddleware` is a dispatch-shape middleware, not
                # an ASGI app - registering it as ASGI would wire the app
                # in as its `dispatch` and fail at request time.
                raise TypeError(
                    f"{middleware.__name__} is a BaseHTTPMiddleware "
                    "(dispatch-shape) - register it with add_http_middleware(), "
                    "not add_middleware()."
                )
            else:
                # A standard ASGI middleware class - it needs the app it
                # wraps, so defer construction until the stack is built.
                self._asgi_middleware.append((middleware, options))
                self._asgi_stack = None
        elif isinstance(middleware, Middleware):
            self._middlewares.append(middleware)
        else:
            # A bare ASGI middleware instance cannot be wired up - veloce
            # has to supply the wrapped app, which only the class form
            # allows.
            raise TypeError(
                f"add_middleware() received a {type(middleware).__name__} instance; "
                "pass a Middleware instance, a Middleware subclass, or an ASGI "
                "middleware *class* (so veloce can supply the wrapped app). "
                "Register a BaseHTTPMiddleware via add_http_middleware()."
            )

    def add_instrumentation(self, hook: Callable) -> Callable:
        """Register an observability instrumentation hook.

        `hook` is called once per finished HTTP request with a
        `RequestMetrics` record - the request method, the concrete path,
        the matched route *template* (a low-cardinality metric label), the
        status code, and the wall-clock duration in milliseconds. It may be
        a plain function or a coroutine function. A hook that raises is
        logged and skipped, so instrumentation never breaks a response.

        Returns `hook` unchanged, so it also works as a decorator:

            @app.add_instrumentation
            def export(metrics):
                statsd.timing(metrics.route or "unmatched", metrics.duration_ms)

        With no hook registered the request path carries no instrumentation
        cost - not even a clock read.
        """
        self._instrumentation.append(hook)
        return hook

    def use_secure_defaults(self) -> None:
        """Apply a security-hardened configuration baseline.

        - Marks the session cookie `Secure`, `HttpOnly`, and (unless
          already configured) `SameSite=Lax`.
        - Registers `SecurityHeadersMiddleware` - `nosniff`, frame-deny,
          a referrer policy, and a one-year HSTS max-age - unless one is
          already present.

        Call once after construction, before serving. Production-oriented:
        the `Secure` cookie flag means cookies are not sent over plain
        HTTP, so do not call this for local HTTP development.
        """
        self.config["SESSION_COOKIE_SECURE"] = True
        self.config["SESSION_COOKIE_HTTPONLY"] = True
        if self.config.get("SESSION_COOKIE_SAMESITE") is None:
            self.config["SESSION_COOKIE_SAMESITE"] = "Lax"
        from veloce.middleware.security import SecurityHeadersMiddleware

        if not any(isinstance(m, SecurityHeadersMiddleware) for m in self._middlewares):
            self.add_middleware(SecurityHeadersMiddleware(hsts_max_age=31536000))

    def security_audit(self) -> list[str]:
        """Return human-readable warnings about the current security posture.

        An empty list means nothing was flagged. Drives the
        `veloce check` CLI command and is also callable directly from a
        pre-deploy script or a test.
        """
        from veloce.middleware.security import SecurityHeadersMiddleware
        from veloce.middleware.sessions import SessionMiddleware

        warnings: list[str] = []
        if self.debug:
            warnings.append("DEBUG is enabled - disable it before deploying to production.")
        if not self.config.get("SECRET_KEY"):
            warnings.append("SECRET_KEY is not set - session signing falls back to weak defaults.")
        has_session = any(isinstance(m, SessionMiddleware) for m in self._middlewares)
        if has_session and not self.config.get("SESSION_COOKIE_SECURE"):
            warnings.append(
                "SESSION_COOKIE_SECURE is off - the session cookie can be sent over plain HTTP."
            )
        if not any(isinstance(m, SecurityHeadersMiddleware) for m in self._middlewares):
            warnings.append(
                "No SecurityHeadersMiddleware registered - responses ship without hardening "
                "headers (call app.use_secure_defaults())."
            )
        return warnings

    @property
    def json(self) -> Any:
        """Active `JSONProvider` instance.

        Lazily instantiated from `app.json_provider_class` so swapping
        encoders is just: `app.json_provider_class = MyJSONProvider`.
        Setting `app.json = instance` replaces it explicitly.
        """
        if self._json_provider is None:
            self._json_provider = self.json_provider_class(self)
        return self._json_provider

    @json.setter
    def json(self, provider: Any) -> None:
        self._json_provider = provider

    def send_static_file(self, filename: str) -> Any:
        """Serve a file from `app.static_folder`.

        `app.static_folder` defaults to `"static"` (relative to
        `app.package_root`). Use `app.static_url_path` to control the
        URL prefix when mounting via `app.static(...)`. Returns a
        `FileResponse`; traversal-safe via `safe_join`.

        This reads the file synchronously and emits a
        `DeprecationWarning` when called on a running loop. From async
        handlers, prefer `send_static_file_async`.
        """
        import os

        from veloce.helpers import send_from_directory

        directory = self.static_folder
        if not os.path.isabs(directory):
            directory = os.path.join(self.package_root, directory)
        return send_from_directory(directory, filename)

    async def send_static_file_async(self, filename: str) -> Any:
        """Serve a file from `app.static_folder` - async variant.

        Reads the file in an executor via `send_from_directory_async`, so
        it never blocks the event loop. Prefer this from async handlers
        over the sync `send_static_file`.
        """
        import os

        from veloce.helpers import send_from_directory_async

        directory = self.static_folder
        if not os.path.isabs(directory):
            directory = os.path.join(self.package_root, directory)
        return await send_from_directory_async(directory, filename)

    @property
    def package_root(self) -> str:
        """Filesystem path of the directory containing `import_name`'s module.

        Veloce exposes this as `app.root_path`; veloce already uses
        `Veloce.root_path` for the ASGI mount prefix, so we surface the
        package-directory variant under a non-conflicting name. Useful
        for resolving template / static directories relative to the
        app's source file.
        """
        import os
        import sys

        mod = sys.modules.get(self.import_name)
        mod_file = getattr(mod, "__file__", None) if mod else None
        if mod_file:
            return os.path.dirname(os.path.abspath(mod_file))
        return os.getcwd()

    @property
    def jinja_env(self) -> Any:
        """The app's shared Jinja2 `Environment`.

        Available once a `template_folder` has been configured (either
        via the constructor or by binding `Jinja2Templates`). Mutate it
        directly to register filters/globals or tweak settings:
        `app.jinja_env.filters["money"] = fmt`. Raises `RuntimeError`
        when no templating is configured.
        """
        if self._templates is None:
            raise RuntimeError(
                "no Jinja environment - pass `template_folder=` to Veloce(...) "
                "or bind a Jinja2Templates instance first"
            )
        return self._templates.env

    @property
    def jinja_loader(self) -> Any:
        """The app's Jinja template loader.

        The `FileSystemLoader` (or whatever loader the bound
        `Jinja2Templates` env uses). `None` when no templating is
        configured - Veloce returns `None` for an app with no template
        folder rather than raising.
        """
        if self._templates is None:
            return None
        return self._templates.env.loader

    @property
    def instance_path(self) -> str:
        """Writable instance folder beside the package.

        Veloce resolves `<package_root>/instance` as a per-deployment
        writable directory for config, SQLite files, uploads, etc.
        An explicit `instance_path=` constructor argument overrides
        this computed default. The directory is *not* auto-created -
        the caller decides whether to `mkdir` it.
        """
        import os

        if self._instance_path is not None:
            return self._instance_path
        return os.path.join(self.package_root, "instance")

    @property
    def signal_namespace(self) -> Any:
        """Accessor that returns the `veloce.signals` module.

        Veloce ships its signals as module-level singletons, so this
        attribute returns the module - `app.signal_namespace.request_started`
        is the same `Signal` instance as `veloce.signals.request_started`.
        """
        from veloce import signals

        return signals

    @property
    def aborter(self) -> Any:
        """Callable that raises typed `HTTPException`s by status code.

        `app.aborter(404)` is equivalent to the module-level
        `abort(404)` helper. It is a distinct attribute so applications
        can subclass `Aborter` to add custom code-to-exception
        mappings; veloce returns a fresh `Aborter` instance per access
        so users can mutate `_mapping` per-app without affecting others.
        """
        from veloce.helpers import Aborter  # breaks app -> exceptions -> helpers cycle

        if self._aborter is None:
            self._aborter = Aborter()
        return self._aborter

    @aborter.setter
    def aborter(self, value: Any) -> None:
        self._aborter = value

    @property
    def got_first_request(self) -> bool:
        """`True` after the first request has been fully handled.

        compatibility - read-only. Useful when conditional setup
        depends on whether the app has bootstrapped yet, e.g. a
        `before_first_request` hook firing exactly once is reflected
        here as `True`.
        """
        return self._first_request_fired

    @property
    def cli(self) -> Any:
        """Click `Group` for app-defined custom CLI commands.

        Accessing `app.cli` lazily constructs a `click.Group` once.
        Custom commands attach via the standard Click decorator:

            @app.cli.command("init-db")
            def init_db():
                ...

        The `veloce` console script automatically discovers and mounts
        the group as a `custom` subcommand when launched with an app
        reference. `click` is required at access time but not at import
        time - the `ImportError` is deferred and produces a useful
        message instead of a hard-import crash on environments that
        don't need the CLI.
        """
        if getattr(self, "_cli_group", None) is None:
            try:
                import click
            except ImportError as err:  # pragma: no cover
                raise RuntimeError(
                    "app.cli requires `click` - install with: pip install click"
                ) from err
            self._cli_group = click.Group(
                name=getattr(self, "title", "app").lower().replace(" ", "-"),
                help=f"Custom CLI for {self.title}.",
            )
        return self._cli_group

    def test_cli_runner(self, **kwargs: Any) -> Any:
        """Return a Click `CliRunner` bound for testing `app.cli`.

        Veloce exposes this for unit-testing `@app.cli.command(...)`
        handlers without manual Click import. Kwargs flow through to
        `click.testing.CliRunner`.
        """
        try:
            from click.testing import CliRunner
        except ImportError as err:  # pragma: no cover
            raise RuntimeError(
                "test_cli_runner() requires `click`. Install with: pip install click"
            ) from err
        return CliRunner(**kwargs)

    # Veloce exposes the internal dispatcher under two names downstream
    # extension code reaches for. Both alias `_dispatch_request`.
    # `full_dispatch_request` runs the full before/after_request chain
    # - which `_dispatch_request` already does inline - so both names
    # point at the same method.
    async def dispatch_request(self, request: Request) -> Any:
        """an alias for `_dispatch_request`."""
        return await self._dispatch_request(request)

    async def full_dispatch_request(self, request: Request) -> Any:
        """an alias for `_dispatch_request` (which already runs the
        full before/after-request hook chain inline)."""
        return await self._dispatch_request(request)

    async def preprocess_request(self, request: Request) -> Any:
        """Run all `before_request` hooks for `request`.

        Walks the registered hooks in order; if any hook returns a
        non-None value it short-circuits the chain and that value is
        returned (the contract - a non-None return becomes the
        response). Both sync and async hooks are supported. App-level
        hooks fire first, then the matched-blueprint bucket - the
        same shape `_dispatch_request` uses.
        """
        for hook in self._before_request_hooks:
            result = await self._call_handler(hook, {"request": request})
            if result is not None:
                return result
        bp = _endpoint_blueprint(getattr(request, "endpoint", None))
        if bp is not None and self._bp_before_hooks:
            for hook in self._bp_before_hooks.get(bp, ()):
                result = await self._call_handler(hook, {"request": request})
                if result is not None:
                    return result
        return None

    async def process_response(self, request: Request, response: Any) -> Any:
        """Run all `after_request` hooks for `(request, response)`.

        Hooks fire in **reverse** registration order; each hook may
        return a replacement response (the contract: a None return
        keeps the existing response). App-level hooks reverse-iterate
        first, then the matched-blueprint bucket - mirrors
        `_dispatch_request`'s ordering.
        """
        for hook in reversed(self._after_request_hooks):
            new = await self._call_handler(hook, {"request": request, "response": response})
            if new is not None:
                response = new
        bp = _endpoint_blueprint(getattr(request, "endpoint", None))
        if bp is not None and self._bp_after_hooks:
            for hook in reversed(self._bp_after_hooks.get(bp, ())):
                new = await self._call_handler(hook, {"request": request, "response": response})
                if new is not None:
                    response = new
        return response

    @staticmethod
    def ensure_sync(func: Callable) -> Callable:
        """Wrap `func` so it is callable from synchronous code.

        - If `func` is a regular function, returns it unchanged.
        - If `func` is a coroutine function, returns a sync wrapper
          that runs the coroutine to completion on a dedicated event
          loop and returns the result.

        Use to bridge async handlers / hooks into sync code (CLI
        commands, background workers, test scaffolding).
        """
        if not _is_async_callable(func):
            return func

        @functools.wraps(func)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return asyncio.run(func(*args, **kwargs))

        return _sync_wrapper

    def make_response(self, value: Any) -> Response:
        """Coerce a handler-return value into a `Response`.

        Accepts (with this coercion table):
        - `Response` -> returned as-is
        - `str` / `bytes` -> wrapped as a text/HTML response
        - `dict` / `list` -> wrapped as a JSON response via `jsonify`
        - `tuple` of `(body,)`, `(body, status)`, `(body, status, headers)`,
          or `(body, headers)` -> unpacked and re-coerced
        """
        from veloce.helpers import jsonify

        if isinstance(value, Response):
            return value
        if isinstance(value, tuple):
            body: Any = value[0]
            status: int | None = None
            headers: Any = None
            if len(value) == 2:
                if isinstance(value[1], int):
                    status = value[1]
                else:
                    headers = value[1]
            elif len(value) == 3:
                status, headers = value[1], value[2]
            resp = self.make_response(body)
            if status is not None:
                resp.status_code = status
            if headers:
                items = headers.items() if isinstance(headers, dict) else headers
                for k, v in items:
                    resp.headers[k] = v
            return resp
        if isinstance(value, (dict, list)):
            return jsonify(value)
        if isinstance(value, bytes):
            return Response(body=value, content_type=MIME_HTML)
        if isinstance(value, str):
            return Response(
                body=value.encode("utf-8"),
                content_type=MIME_HTML,
            )
        raise TypeError(f"Cannot coerce {type(value).__name__} to Response")

    def test_client(self, **kwargs: Any) -> Any:
        """Return an in-memory `TestClient` for this app.

        `app.test_client()` is the factory API; the kwargs (e.g.
        `follow_redirects=True`, `base_url=...`) are forwarded to
        `TestClient.__init__`. Equivalent to `TestClient(app, **kwargs)`
        for callers that prefer the method form.
        """
        from veloce.testclient import TestClient

        return TestClient(self, **kwargs)

    def async_test_client(self, **kwargs: Any) -> Any:
        """Return an `AsyncTestClient` for this app.

        The async counterpart of `test_client()` - used as
        `async with app.async_test_client() as client:` inside an async
        test, so requests are awaited on the test's own running loop
        rather than driven through a private loop. Kwargs are forwarded
        to `AsyncTestClient.__init__`.
        """
        from veloce.testclient import AsyncTestClient

        return AsyncTestClient(self, **kwargs)

    def app_context(self) -> _AppContext:
        """Bind `current_app` and reset `g` for use outside a request.

        Use as `with app.app_context(): ...`. CLI commands, background
        jobs, and tests need this when they want to read `app.config` or
        write into `g` without going through `handle_request`. Nestable:
        the previous binding (if any) is restored on exit.
        """
        return _AppContext(self)

    def test_request_context(
        self,
        path: str = "/",
        method: str = HTTP_METHOD_GET,
        headers: dict[str, str] | None = None,
        query_string: str = "",
        body: bytes = b"",
    ) -> _TestRequestContext:
        """Synthesise a fake request for outside-request testing.

        Inside `with app.test_request_context(): ...`, `current_app`, `g`,
        and the request-scoped contextvars resolve as if Veloce
        had just received that request - without spinning up the full
        dispatch pipeline. Strict subset of what `handle_request` does:
        no middleware, no DI, no handler.
        """
        return _TestRequestContext(
            self,
            path=path,
            method=method,
            headers=headers or {},
            query_string=query_string,
            body=body,
        )

    def add_http_middleware(self, middleware: Any) -> Any:
        """Register a `BaseHTTPMiddleware`-style middleware on the
        `(request, call_next) -> response` chain. Accepts an instance, a
        bare callable, or a class (which is instantiated with no args).
        Returns the registered object so it can be used as a decorator.
        """
        # Class -> instance.
        if isinstance(middleware, type):
            middleware = middleware()
        if not callable(middleware):
            raise TypeError(
                f"add_http_middleware expects a callable / instance / class, got {middleware!r}"
            )
        self._http_middleware_funcs.append(middleware)
        return middleware

    def middleware(self, middleware_class_or_type: type | str, **kwargs) -> Any:
        """Add middleware - supports both a class form and a decorator form.

        Class form: app.middleware(CORSMiddleware, allow_origins=["*"])
        Decorator form:
            @app.middleware("http")
            async def add_header(request, call_next):
                response = await call_next(request)
                response.headers["X-Custom"] = "value"
                return response
        """
        if isinstance(middleware_class_or_type, str) and middleware_class_or_type == "http":

            def decorator(func: Callable) -> Callable:
                self._http_middleware_funcs.append(func)
                return func

            return decorator
        else:
            if isinstance(middleware_class_or_type, str):
                raise TypeError(
                    "middleware(middleware_class) is the class form; "
                    "@app.middleware('http') is the decorator form"
                )
            self.add_middleware(middleware_class_or_type, **kwargs)

    # -- Exception handlers ---------------------------------------

    def register_error_handler(self, code_or_exception: int | type, func: Callable) -> None:
        """Register an error handler without a decorator."""
        if isinstance(code_or_exception, int):
            self._status_handlers[code_or_exception] = func
        else:
            self._exception_handlers[code_or_exception] = func
            # The MRO-walk cache is invalidated on any registration so a
            # newly-added handler for a base class takes effect for the
            # already-cached subclasses.
            self._exc_handler_cache.clear()

    def _should_propagate_exceptions(self) -> bool:
        """Whether unhandled exceptions should re-raise out of dispatch.

        True when `app.config["PROPAGATE_EXCEPTIONS"]` is explicitly set,
        or implicitly when both DEBUG and TESTING are enabled.
        """
        explicit = self.config.get("PROPAGATE_EXCEPTIONS")
        if explicit is not None:
            return bool(explicit)
        return self.debug and _coerce_bool(self.config.get("TESTING"))

    def _find_exception_handler(self, exc_type: type) -> Callable | None:
        """Walk `exc_type`'s MRO looking for a registered handler.

        Handlers registered against a base class catch every subclass -
        e.g. `@app.exception_handler(HTTPException)` catches every
        `NotFound`, `Forbidden`, etc. raised through `abort()`. The
        lookup result is cached per exception type; the cache is cleared
        on every `register_error_handler` call.
        """
        cached = self._exc_handler_cache.get(exc_type, _MISSING)
        if cached is not _MISSING:
            return cached
        for cls in exc_type.__mro__:
            handler = self._exception_handlers.get(cls)
            if handler is not None:
                self._exc_handler_cache[exc_type] = handler
                return handler
        self._exc_handler_cache[exc_type] = None
        return None

    def exception_handler(self, exc_class_or_status: type | int) -> Callable:
        """Register a custom exception handler by exception type or status code."""

        def decorator(func: Callable) -> Callable:
            self.register_error_handler(exc_class_or_status, func)
            return func

        return decorator

    # Veloce names this `errorhandler` (one word, no underscore). The
    # alias keeps calling code readable; semantics are identical.
    errorhandler = exception_handler

    def add_exception_handler(self, exc_class_or_status: type | int, handler: Callable) -> None:
        """Imperative exception-handler registration - ASGI shape.

        The non-decorator form of `@app.exception_handler(...)`.
        Accepts an exception class (matched by MRO at dispatch time) or
        an int HTTP status code.
        """
        self.register_error_handler(exc_class_or_status, handler)

    def log_exception(self, exc: BaseException) -> None:
        """Log an exception with traceback.

        Routes the exception through the app logger at ERROR level.
        Used internally before falling back to a 500 response; exposed
        publicly so error-handler code can re-log via the same path.
        """
        self.logger.error("Exception on request", exc_info=exc)

    async def handle_http_exception(
        self, exc: HTTPException, request: Request | None = None
    ) -> Response:
        """Build the response for an `HTTPException`.

        Walks registered status-code + class handlers first (matching
        `abort()` semantics), falling back to JSON `{"detail": exc.detail}`
        with `exc.headers` applied. Useful for code paths outside the
        normal request cycle (e.g. background tasks) that want
        framework-consistent error shapes.

        Pass `request=` when calling from inside a request scope so the
        registered error handler receives the real failing request
        (with the actual `path`, `method`, `path_params`, `state`, etc.)
        instead of a synthetic `GET /`. Callers without a request (the
        original out-of-band use case) can omit it.
        """
        handler = self._status_handlers.get(exc.status_code) or self._find_exception_handler(
            type(exc)
        )
        if handler is not None:
            if request is None:
                from veloce.http.request import Request as _Req

                request = _Req(
                    method=HTTP_METHOD_GET, path="/", query_string="", headers={}, body=b""
                )
            result = await self._call_exc_handler(handler, request, exc)
            if isinstance(result, Response):
                return result
            return self._coerce_response(result)
        structured = getattr(exc, "errors", None)
        return JSONResponse(
            {"detail": structured if structured is not None else (exc.detail or "Error")},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    def make_default_options_response(
        self, path: str, allowed_methods: list[str] | None = None
    ) -> Response:
        """Build the auto-OPTIONS response for `path`.

        Returns a 200 response with an empty body and an `Allow` header
        listing every method registered for `path`, augmented with
        `HEAD` (whenever `GET` is supported) and `OPTIONS` itself per
        RFC 9110 Sec. 9.3.7. Callers that register an explicit OPTIONS
        handler can use this to compose the default `Allow` set. Pass
        `allowed_methods` when the registered set is already known to skip
        the redundant `get_allowed_methods` lookup.
        """
        allowed = allowed_methods if allowed_methods is not None else self.get_allowed_methods(path)
        advertised = list(allowed)
        if HTTP_METHOD_GET in advertised and HTTP_METHOD_HEAD not in advertised:
            advertised.append(HTTP_METHOD_HEAD)
        if HTTP_METHOD_OPTIONS not in advertised:
            advertised.append(HTTP_METHOD_OPTIONS)
        return Response(
            status_code=status.HTTP_200_OK,
            body=b"",
            content_type=MIME_TEXT_PLAIN,
            headers={HEADER_ALLOW: ", ".join(advertised)},
        )

    async def handle_user_exception(
        self, exc: BaseException, request: Request | None = None
    ) -> Response:
        """Dispatch an arbitrary exception.

        `HTTPException` -> `handle_http_exception`. Otherwise walks
        registered class handlers (MRO); on no match, logs via
        `log_exception` and returns 500. Pass `request=` to propagate
        the real failing request to the registered handler; omit to
        get a synthetic `GET /` for out-of-band callers (background
        tasks, CLI hooks).
        """
        if isinstance(exc, HTTPException):
            return await self.handle_http_exception(exc, request=request)
        handler = self._find_exception_handler(type(exc))
        if handler is not None:
            if request is None:
                from veloce.http.request import Request as _Req

                request = _Req(
                    method=HTTP_METHOD_GET, path="/", query_string="", headers={}, body=b""
                )
            result = await self._call_exc_handler(handler, request, exc)
            if isinstance(result, Response):
                return result
            return self._coerce_response(result)
        self.log_exception(exc)
        return JSONResponse(
            {"detail": MSG_INTERNAL_SERVER_ERROR},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    @property
    def view_functions(self) -> dict[str, Callable]:
        """A `{endpoint_name: handler}` view of registered routes.

        Endpoint names follow a simple rule - the route's `name=`
        kwarg, or the handler's `__name__` when no name is set; blueprint
        routes are prefixed with `<bpname>.`. Returned dict is a fresh
        snapshot - mutation doesn't poison framework state.
        """
        cached = self._cached_view_functions
        if cached is None:
            cached = {}
            for _method, _path, info in self._collect_all_routes():
                cached[info.name] = info.handler
            self._cached_view_functions = cached
        return dict(cached)

    def endpoint(self, name: str) -> Callable:
        """Decorator attaching a function as the view for `name`
        on an already-registered route.

        Useful when separating route declaration (via
        `app.add_url_rule(rule, endpoint="x")`) from view registration.
        Replaces the existing route's handler in place.
        """

        def decorator(func: Callable) -> Callable:
            replaced = False
            for _method, _path, info in self._collect_all_routes():
                if info.name == name:
                    info.handler = func
                    info.description = info.description or (func.__doc__ or "")
                    replaced = True
                    # Recompute the pre-built handler plan since the
                    # callable changed.
                    try:
                        from veloce._handler_plan import K_REQUEST, build_plan

                        plan = build_plan(func)
                        info.handler_plan = plan
                        info.is_trivial_plan = plan is not None and len(plan.slots) == 0
                        info.is_request_only_plan = (
                            plan is not None
                            and len(plan.slots) == 1
                            and plan.slots[0].kind == K_REQUEST
                        )
                    except Exception:
                        info.handler_plan = None
                        info.is_trivial_plan = False
                        info.is_request_only_plan = False
            if not replaced:
                raise ValueError(f"No route registered for endpoint {name!r}")
            return func

        return decorator

    @property
    def error_handler_spec(self) -> dict[Any, dict[Any, Callable]]:
        """Inspection view of registered error handlers.

        Returns a `{blueprint_name_or_None: {key: handler}}` mapping.
        veloce keeps a flat registry (no per-blueprint sub-tables -
        blueprint handlers are merged into the app's dicts at
        `register_blueprint` time), so this view always carries a
        single `None` key whose value contains every registered
        handler keyed by integer status code or exception class.
        """
        merged: dict[Any, Callable] = {}
        merged.update(self._status_handlers)
        merged.update(self._exception_handlers)
        return {None: merged}

    @property
    def before_request_funcs(self) -> dict[Any, list[Callable]]:
        """View of registered `before_request` hooks.

        Returns `{blueprint_name_or_None: [hook, ...]}`. App-level hooks
        live under the `None` key; blueprint hooks under the blueprint's
        name. The dispatcher walks the `None` bucket plus the bucket
        whose name matches the matched route's endpoint prefix.
        """
        result: dict[Any, list[Callable]] = {None: list(self._before_request_hooks)}
        for bp, hooks in self._bp_before_hooks.items():
            result[bp] = list(hooks)
        return result

    @property
    def after_request_funcs(self) -> dict[Any, list[Callable]]:
        """Return the per-blueprint after-request hook registry."""
        result: dict[Any, list[Callable]] = {None: list(self._after_request_hooks)}
        for bp, hooks in self._bp_after_hooks.items():
            result[bp] = list(hooks)
        return result

    @property
    def teardown_request_funcs(self) -> dict[Any, list[Callable]]:
        """Return the per-blueprint teardown-request hook registry."""
        result: dict[Any, list[Callable]] = {None: list(self._teardown_request_hooks)}
        for bp, hooks in self._bp_teardown_hooks.items():
            result[bp] = list(hooks)
        return result

    @property
    def blueprints(self) -> dict[str, Any]:
        """snapshot mapping of `bp.name -> Blueprint`.

        Returns a fresh copy, so caller mutations don't affect the
        framework. Re-registering the same name overwrites the previous
        entry.
        """
        return dict(self._blueprints_map)

    def iter_blueprints(self) -> Any:
        """iterate over every registered `Blueprint`.

        Returns the blueprints in registration order (Python 3.7+ dict
        insertion order). Yields the Blueprint objects, not their names.
        """
        return iter(self._blueprints_map.values())

    @property
    def url_value_preprocessors(self) -> dict[Any, list[Callable]]:
        """View of registered URL-value preprocessors.

        Returns `{blueprint_name_or_None: [fn, ...]}`. Veloce flattens
        blueprint preprocessors into the app list at registration time,
        so the dict carries a single `None` key.
        """
        return {None: list(self._url_value_preprocessors)}

    @property
    def url_default_functions(self) -> dict[Any, list[Callable]]:
        """View of registered URL-default callbacks."""
        return {None: list(self._url_default_funcs)}

    # -- Before/After request hooks -------------------------------

    def before_request(self, func: Callable) -> Callable:
        """Register a function to run before each request."""
        self._before_request_hooks.append(func)
        return func

    def shell_context_processor(self, func: Callable) -> Callable:
        """Register a function returning a dict to merge into `veloce shell`.

        each processor is called with no args; its dict
        becomes part of the namespace the interactive shell starts with.
        Useful for surfacing models / db sessions / common helpers so
        `User.query.first()` works without a manual `from myapp.models
        import User` every time.
        """
        self._shell_context_processors.append(func)
        return func

    def make_shell_context(self) -> dict[str, Any]:
        """Build the dict the CLI's `shell` command drops into.

        Always includes `app` (this Veloce instance) and `g`. Each
        registered shell-context-processor's return dict overlays on
        top, in registration order - later processors win on conflicts.
        """
        from veloce.helpers import g

        ctx: dict[str, Any] = {"app": self, "g": g}
        for fn in self._shell_context_processors:
            extra = fn()
            if extra:
                ctx.update(extra)
        return ctx

    def before_first_request(self, func: Callable) -> Callable:
        """Register a function to run exactly once on the first request.

        A legacy hook style - lifespan startup handlers are preferred,
        but first-request hooks are still a common pattern,
        so both are supported. Hooks fire serially in registration
        order; single-fire is guarded with an `asyncio.Lock` so
        concurrent first requests don't double-run the callbacks.
        """
        self._before_first_request_hooks.append(func)
        return func

    def after_request(self, func: Callable) -> Callable:
        """Register a function to run after each request."""
        self._after_request_hooks.append(func)
        return func

    def teardown_request(self, func: Callable) -> Callable:
        """Register a function to run after request teardown.
        Called with an optional exception argument, even if an exception occurred."""
        self._teardown_request_hooks.append(func)
        return func

    def teardown_appcontext(self, func: Callable) -> Callable:
        """Register a function to run on app-context teardown."""
        self._teardown_appcontext_hooks.append(func)
        return func

    async def _run_teardown_hooks(
        self, hooks: list[Callable], exc: BaseException | None, label: str
    ) -> None:
        """Run a list of teardown hooks, logging but never raising errors."""
        for hook in hooks:
            try:
                if _is_async_callable(hook):
                    await hook(exc)
                else:
                    loop = asyncio.get_running_loop()
                    ctx = contextvars.copy_context()
                    await loop.run_in_executor(None, ctx.run, functools.partial(hook, exc))
            except Exception:
                self.logger.exception(f"{label} hook raised an exception")

    def context_processor(self, func: Callable) -> Callable:
        """Register a template context processor.
        The function should return a dict that merges into the template context."""
        self._context_processors.append(func)
        return func

    # -- Jinja2 helper registration -------------------------------

    def template_filter(self, name: str | None = None) -> Callable:
        """Register a function as a Jinja filter.

        Usage:
            @app.template_filter("upper")
            def upper(s): return s.upper()

        The filter becomes available in every `Jinja2Templates` render that
        runs inside this app's request scope. `name` defaults to the
        function's own `__name__`.
        """

        def decorator(func: Callable) -> Callable:
            filter_name = name or func.__name__
            self._template_filters.append((filter_name, func))
            return func

        return decorator

    def template_global(self, name: str | None = None) -> Callable:
        """Register a callable as a Jinja global - accessible from any
        template by name. Same shape as `template_filter`."""

        def decorator(func: Callable) -> Callable:
            global_name = name or func.__name__
            self._template_globals.append((global_name, func))
            return func

        return decorator

    def add_template_global(self, func: Callable, name: str | None = None) -> None:
        """Imperative equivalent of `@template_global`."""
        self._template_globals.append((name or func.__name__, func))

    def template_test(self, name: str | None = None) -> Callable:
        """Register a Jinja test - used in `{% if x is name %}` constructs."""

        def decorator(func: Callable) -> Callable:
            test_name = name or func.__name__
            self._template_tests.append((test_name, func))
            return func

        return decorator

    def add_template_filter(self, func: Callable, name: str | None = None) -> None:
        """Imperative equivalent of `@template_filter`."""
        self._template_filters.append((name or func.__name__, func))

    def add_template_test(self, func: Callable, name: str | None = None) -> None:
        """Imperative equivalent of `@template_test`."""
        self._template_tests.append((name or func.__name__, func))

    def update_template_context(self, context: dict[str, Any]) -> dict[str, Any]:
        """Merge registered context-processor output into `context`.

        Runs every `@app.context_processor` callback and folds the
        returned dicts into `context` **in place**, without overriding
        keys the caller already set (the documented semantics - explicit context
        wins). Returns the same dict for chaining.
        """
        for processor in self._context_processors:
            result = processor()
            if isinstance(result, dict):
                for k, v in result.items():
                    context.setdefault(k, v)
        return context

    # -- URL processors (URL hooks) -----------------------------

    def url_value_preprocessor(self, func: Callable) -> Callable:
        """Register a function `fn(endpoint, values)` that can mutate the
        matched path params before the handler runs.

        Usage:
            @app.url_value_preprocessor
            def pull_lang(endpoint, values):
                from veloce import g
                g.lang = values.pop("lang", "en")

        `endpoint` is the route name; `values` is the path_params dict
        (mutating it in place is the supported way to remove / rewrite
        values before the handler sees them).
        """
        self._url_value_preprocessors.append(func)
        return func

    def url_for(self, name: str, **path_params: Any) -> str:
        """`Veloce.url_for` runs `@app.url_defaults` callbacks before
        delegating to `Router.url_for`, so injected defaults appear in the
        rendered URL.

        On build failure (unknown endpoint or missing path parameter),
        each registered `app.url_build_error_handlers` callback is
        invoked with `(error, endpoint, values)` in order; the first
        non-None return is used. If none recovers, a `BuildError` is
        raised.
        """
        from veloce.exceptions import BuildError

        if self._url_default_funcs:
            # Copy so the callbacks can mutate without changing the caller's
            # kwargs dict.
            values = dict(path_params)
            for fn in self._url_default_funcs:
                fn(name, values)
        else:
            values = path_params

        try:
            return super().url_for(name, **values)
        except (ValueError, KeyError) as exc:
            err = BuildError(name, values)
            err.__cause__ = exc
            for handler in self.url_build_error_handlers:
                result = handler(err, name, values)
                if result is not None:
                    return result
            raise err from exc

    # Keep `url_path_for` aligned with the override above.
    def url_path_for(self, name: str, **path_params: Any) -> str:
        """Resolve a URL path by endpoint name and parameters."""
        return self.url_for(name, **path_params)

    def url_defaults(self, func: Callable) -> Callable:
        """Register a function `fn(endpoint, values)` that injects default
        kwargs into every `url_for` / `url_path_for` call.

        Usage:
            @app.url_defaults
            def add_lang(endpoint, values):
                from veloce import g
                values.setdefault("lang", g.get("lang", "en"))

        Runs in registration order; mutate `values` in place.
        """
        self._url_default_funcs.append(func)
        return func

    def register_blueprint(
        self,
        blueprint: Any,
        url_prefix: str | None = None,
    ) -> None:
        """Mount a `Blueprint`'s routes + hooks onto this app.

        - Re-registers each route under `(url_prefix or bp.url_prefix) + path`
          so the same blueprint can be mounted twice (e.g. v1/v2 versions).
        - Splices the blueprint's `before_request` / `after_request` /
          `teardown_request` hooks into the app's own lists. Blueprint
          hooks fire only for blueprint-routed requests (gated via
          `request.endpoint` starting with `"<bpname>."`); we tag the
          blueprint's hooks so the dispatcher can filter.
        - Splices blueprint-level error handlers into the app's tables;
          app-level handlers take precedence on conflicts because
          they're already registered.

        Mountable multiple times on different apps with different
        prefixes - the blueprint itself stays unmodified.
        """
        from veloce.blueprints import Blueprint

        if not isinstance(blueprint, Blueprint):
            raise TypeError(
                f"register_blueprint expects a Blueprint, got {type(blueprint).__name__}"
            )

        effective_prefix = url_prefix if url_prefix is not None else blueprint.url_prefix
        bp_name = blueprint.name
        # Stash the blueprint under its registered name so `app.blueprints`
        # and `app.iter_blueprints()` can return it later. Re-registration
        # under the same name overwrites.
        self._blueprints_map[bp_name] = blueprint

        # Splice each route onto the app's tree under the combined prefix.
        for path, methods, info in blueprint._walk_routes():
            full_path = (effective_prefix or "") + path
            # The route's name is prefixed with `<bpname>.` so url_for
            # and dispatcher hook-gating can find it.
            endpoint = f"{bp_name}.{info.name}"
            self.add_route(
                path=full_path,
                handler=info.handler,
                methods=methods,
                dependencies=info.dependencies,
                response_model=info.response_model,
                tags=info.tags,
                summary=info.summary,
                name=endpoint,
                description=info.description,
                deprecated=info.deprecated,
                response_description=info.response_description,
                status_code=info.status_code,
                response_class=info.response_class,
                response_model_include=info.response_model_include,
                response_model_exclude=info.response_model_exclude,
                response_model_exclude_unset=info.response_model_exclude_unset,
                response_model_exclude_defaults=info.response_model_exclude_defaults,
                response_model_by_alias=info.response_model_by_alias,
                response_model_exclude_none=info.response_model_exclude_none,
                include_in_schema=info.include_in_schema,
                responses=info.responses,
                operation_id=info.operation_id,
                openapi_extra=info.openapi_extra,
                defaults=info.defaults,
                callbacks=info.callbacks,
                subdomain=info.subdomain,
                host=info.host,
            )

        # Bucket the blueprint's hooks under its name so dispatch can
        # look them up by the matched route's endpoint prefix instead of
        # walking every blueprint's gated wrapper on every request.
        # Previously: a `_gate` closure per hook in the flat
        # `_before_request_hooks` list did a `req.endpoint.startswith(...)`
        # check on every hook for every request - O(B*H) no-op work for
        # apps with many blueprints. Now the dispatcher reads
        # `_bp_before_hooks[bp_name]` directly.
        if blueprint._before_request_hooks:
            self._bp_before_hooks.setdefault(bp_name, []).extend(blueprint._before_request_hooks)
        if blueprint._after_request_hooks:
            self._bp_after_hooks.setdefault(bp_name, []).extend(blueprint._after_request_hooks)
        if blueprint._teardown_request_hooks:
            self._bp_teardown_hooks.setdefault(bp_name, []).extend(
                blueprint._teardown_request_hooks
            )

        # URL processors (L7) - wrapped so they only fire for endpoints
        # belonging to the blueprint. The endpoint string is the first
        # arg of the `(endpoint, values)` callable.
        url_gate_prefix = f"{bp_name}."

        def _proc_gate(fn: Callable) -> Callable:
            def _gated(endpoint: str, values: dict) -> Any:
                if endpoint and endpoint.startswith(url_gate_prefix):
                    return fn(endpoint, values)
                return None

            return _gated

        for fn in blueprint._url_value_preprocessors:
            self._url_value_preprocessors.append(_proc_gate(fn))
        for fn in blueprint._url_default_funcs:
            self._url_default_funcs.append(_proc_gate(fn))

        # Error handlers: app-level wins on conflict (don't overwrite).
        for exc_cls, handler in blueprint._exception_handlers.items():
            self._exception_handlers.setdefault(exc_cls, handler)
        self._exc_handler_cache.clear()
        for code, handler in blueprint._status_handlers.items():
            self._status_handlers.setdefault(code, handler)

    def add_url_rule(
        self,
        rule: str,
        endpoint: str | None = None,
        view_func: Callable | None = None,
        methods: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """Add a URL rule programmatically.

        `view_func=None` registers an **endpoint-only stub**: the route
        exists for `url_for` resolution but has no handler yet. Attach
        one later with `@app.endpoint(endpoint)`. Calling such a route
        before a handler is attached raises a clear `RuntimeError`.
        Requires `endpoint` to be set in the stub case.
        """
        if view_func is None:
            if not endpoint:
                raise ValueError(
                    "add_url_rule needs `endpoint` when `view_func` is None (endpoint-only stub)"
                )

            async def _stub_view(request: Request, **path_params: Any) -> Any:
                raise RuntimeError(
                    f"endpoint {endpoint!r} has no view function yet - "
                    f"attach one with @app.endpoint({endpoint!r})"
                )

            _stub_view.__name__ = endpoint
            view_func = _stub_view
        self.add_route(
            path=rule,
            handler=view_func,
            methods=methods or [HTTP_METHOD_GET],
            name=endpoint,
            **kwargs,
        )

    # -- Dependency overrides (for testing) ------------------------

    def dependency_overrides_provider(self) -> dict[Callable, Callable]:
        """Return the dependency override mapping."""
        return self._dependency_overrides

    @property
    def dependency_overrides(self) -> dict[Callable, Callable]:
        """Mutable map of dependency callables to test replacements.

        Populate it to swap a real dependency for a fake one in tests::

            app.dependency_overrides[get_db] = get_fake_db

        The resolver consults this map on every request, so changes take
        effect immediately. Assigning a fresh dict (or calling `.clear()`)
        removes all overrides.
        """
        return self._dependency_overrides

    @dependency_overrides.setter
    def dependency_overrides(self, value: dict[Callable, Callable]) -> None:
        self._dependency_overrides = value
        # Sub-plans cached for previous override callables can no longer be
        # reached, but they pin the callable + its plan. Drop them so a
        # long-lived test suite that swaps in hundreds of fakes doesn't leak.
        self._override_subplans.clear()

    # -- Mount sub-applications ------------------------------------

    def mount(self, prefix: str, app: Any) -> None:
        """Mount a sub-application at a path prefix.

        A veloce sub-app is dispatched through the parent's request
        pipeline. Any other ASGI application - an ASGI micro-app, an
        instrumentation shim - is dispatched at the ASGI layer instead:
        the matched prefix is stripped from the scope's `path` and moved
        onto `root_path`, so the mounted app sees a normal root-relative
        request.

        Scope: a mounted ASGI app receives `http` and `websocket` scopes
        only. The parent app owns the `lifespan` cycle and does not fan it
        out, so a mounted app must not depend on ASGI `lifespan` events
        for its setup. A mounted ASGI app owns its entire prefix subtree -
        a native route registered under the same prefix is unreachable.

        Prefixes must not overlap: registering a prefix equal to, nested
        under, or containing an existing mount raises `ValueError`, since
        overlapping mounts would shadow each other in a confusing,
        order-dependent way.
        """
        prefix = prefix.rstrip("/")
        # A request path always starts with "/"; normalise a prefix given
        # without one so the mount is not silently unreachable.
        if prefix and not prefix.startswith("/"):
            prefix = "/" + prefix
        # Reject an overlapping registration. Two prefixes overlap when
        # one is a path-segment ancestor of the other (or they are
        # equal) - mounts are matched in registration order, so an
        # overlap means one mount silently shadows the other.
        for existing, _existing_slash, _ in (*self._mounted_apps, *self._asgi_mounts):
            if (
                prefix == existing
                or prefix.startswith(existing + "/")
                or existing.startswith(prefix + "/")
            ):
                raise ValueError(
                    f"mount prefix {prefix or '/'!r} overlaps the "
                    f"already-mounted prefix {existing or '/'!r}"
                )
        entry = (prefix, prefix + "/", app)
        if isinstance(app, Veloce):
            self._mounted_apps.append(entry)
            return
        # `StaticFiles` looks ASGI-shaped (it's an object you'd
        # naturally hand to `mount`), but it speaks Veloce's
        # `.handle(request)` protocol, not ASGI. Without a special
        # case, `app.mount("/static", StaticFiles(...))` would register
        # successfully and then 500 every request when the ASGI
        # dispatcher tries `await mounted(scope, receive, send)`. Route
        # it through the static-handler list with the mount prefix as
        # the lookup prefix instead.
        if isinstance(app, StaticFiles):
            app.prefix = prefix.rstrip("/")
            self._static_handlers.append(app)
            return
        # Anything else must be callable in the ASGI shape. Catching
        # non-callables here surfaces the mistake at registration
        # instead of as a per-request 500 later.
        if not callable(app):
            raise TypeError(
                f"mount({prefix or '/'!r}, ...) expected an ASGI application "
                f"(callable taking `(scope, receive, send)`), a `Veloce` sub-app, "
                f"or a `StaticFiles` instance - got "
                f"{type(app).__name__} which is none of those. "
                f"For Veloce's own static-file handler, prefer "
                f"`app.mount_static(prefix=..., directory=...)`."
            )
        self._asgi_mounts.append(entry)

    def _match_asgi_mount(self, path: str) -> tuple[str, Any] | None:
        """Return the `(prefix, app)` whose prefix owns `path`, if any."""
        for prefix, prefix_slash, mounted in self._asgi_mounts:
            if path == prefix or path.startswith(prefix_slash):
                return prefix, mounted
        return None

    # -- Lifecycle events -----------------------------------------

    def on_event(self, event: str) -> Callable:
        """Register startup/shutdown event handlers.

        Deprecated: use `@app.on_startup` / `@app.on_shutdown` instead.
        Scheduled for removal in v0.2.0.
        """
        if event not in (LIFECYCLE_STARTUP, LIFECYCLE_SHUTDOWN):
            raise ValueError(
                f"event must be {LIFECYCLE_STARTUP!r} or {LIFECYCLE_SHUTDOWN!r}, got {event!r}"
            )
        warnings.warn(
            "Veloce.on_event() is deprecated and will be removed in v0.2.0; "
            "use @app.on_startup / @app.on_shutdown instead.",
            DeprecationWarning,
            stacklevel=2,
        )

        def decorator(func: Callable) -> Callable:
            if event == LIFECYCLE_STARTUP:
                self._on_startup.append(func)
            elif event == LIFECYCLE_SHUTDOWN:
                self._on_shutdown.append(func)
            return func

        return decorator

    def on_startup(self, func: Callable) -> Callable:
        """Register a startup event handler."""
        self._on_startup.append(func)
        return func

    def on_shutdown(self, func: Callable) -> Callable:
        """Register a shutdown event handler."""
        self._on_shutdown.append(func)
        return func

    def add_event_handler(self, event: str, func: Callable) -> None:
        """Imperative event-handler registration - ASGI shape.

        Deprecated: call `app.on_startup(fn)` / `app.on_shutdown(fn)`
        directly instead. Scheduled for removal in v0.2.0.
        """
        warnings.warn(
            "Veloce.add_event_handler() is deprecated and will be removed "
            "in v0.2.0; use app.on_startup(fn) / app.on_shutdown(fn) instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        if event == LIFECYCLE_STARTUP:
            self._on_startup.append(func)
        elif event == LIFECYCLE_SHUTDOWN:
            self._on_shutdown.append(func)
        else:
            raise ValueError(
                f"event must be {LIFECYCLE_STARTUP!r} or {LIFECYCLE_SHUTDOWN!r}, got {event!r}"
            )

    # Lifespan-event aliases. `before_serving` fires once at app startup
    # (lifespan event); `after_serving` fires once at shutdown. They are
    # semantically equivalent to `on_startup` / `on_shutdown`; both name
    # pairs are accepted so either reads naturally at the call site.
    def before_serving(self, func: Callable) -> Callable:
        """Register a coroutine to run once at app startup."""
        self._on_startup.append(func)
        return func

    def after_serving(self, func: Callable) -> Callable:
        """Register a coroutine to run once at app shutdown."""
        self._on_shutdown.append(func)
        return func

    # -- Static files ---------------------------------------------

    def mount_static(
        self,
        prefix: str = "/static",
        directory: str = "static",
        html: bool = False,
        must_exist: bool = True,
    ) -> None:
        """Mount a static file directory.

        The directory must exist and be readable at wiring time (a typo
        otherwise 404s every asset silently); pass ``must_exist=False`` to
        downgrade the check to a warning when the directory is created after
        the app is constructed.
        """
        self._static_handlers.append(
            StaticFiles(directory=directory, prefix=prefix, html=html, must_exist=must_exist)
        )

    # -- Request handling -----------------------------------------

    async def handle_request(self, request: Request) -> Response:
        """Main request handler - runs middleware chain + route dispatch."""
        # Lazy OpenAPI setup (ensures routes exist on first request regardless of entry point)
        if not self._openapi_setup:
            self._setup_openapi()

        # Inject app reference into request
        request.app = self

        # `current_app` / `request` contextvars + per-request g reset.
        # Letting the contextvar fall through naturally when the request
        # task ends is intentional - async dispatch may span tasks that
        # diverge from a `set/reset` token.
        _current_app_var.set(self)
        _current_request_var.set(request)
        g._reset()

        try:
            request_started.send(self, request=request)
        except Exception:
            self.logger.exception("request_started signal receiver raised")

        # Drain `before_first_request` hooks exactly once. The double-check
        # under the lock is the canonical pattern: the unlocked check
        # short-circuits the common (already-fired) case without acquiring
        # the lock; the locked check guarantees single-fire when concurrent
        # first requests race.
        if not self._first_request_fired and self._before_first_request_hooks:
            if self._first_request_lock is None:
                self._first_request_lock = asyncio.Lock()
            async with self._first_request_lock:
                if not self._first_request_fired:
                    for hook in self._before_first_request_hooks:
                        await self._call_handler(hook, {})
                    self._first_request_fired = True

        # Enforce MAX_CONTENT_LENGTH. Check both the declared
        # Content-Length (cheap reject) and the actually-buffered body size
        # (defence-in-depth when no Content-Length was sent). Per
        # RFC 9110 Sec. 15.5.14, the status is 413 Content Too Large.
        max_size = self.config.get("MAX_CONTENT_LENGTH")
        if max_size is not None:
            declared = request.content_length
            over = declared is not None and declared > max_size
            # For an in-memory request the body is already buffered, so the
            # await resolves immediately and we enforce against the actual
            # bytes (defence-in-depth for bodies that omit Content-Length).
            # For a streamed request (raw HTTP/1.1) the body has NOT arrived
            # yet - draining it here would defeat streaming and force the
            # whole body into memory. The protocol already caps the streamed
            # running total and the body source raises 413 mid-read, so the
            # declared-length check above is the only eager enforcement.
            if not over and request._body_drained:
                buffered = await request.body()
                over = len(buffered) > max_size
            if over:
                response: Response = JSONResponse(
                    {
                        "detail": MSG_REQUEST_BODY_EXCEEDS_MAX,
                        "status_code": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        "limit": max_size,
                    },
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )
                if self._middlewares:
                    response = await self._run_response_middleware(request, response)
                return response

        # Time the dispatch only when instrumentation hooks are registered -
        # an un-instrumented app does not even read the clock.
        instrument = self._instrumentation
        started = time.perf_counter() if instrument else 0.0

        try:
            # If @app.middleware("http") funcs are registered, wrap dispatch
            # in the call_next chain.
            if self._http_middleware_funcs:
                response = await self._run_http_middleware_chain(request)
            else:
                response = await self._dispatch_request(request)
        except Exception:
            # Dispatch propagated an exception (e.g. PROPAGATE_EXCEPTIONS is
            # set). Record a `500` metric before the exception continues
            # out, so error requests are never dropped from observability.
            if instrument:
                with contextlib.suppress(Exception):
                    await self._run_instrumentation(
                        request,
                        status.HTTP_500_INTERNAL_SERVER_ERROR,
                        (time.perf_counter() - started) * 1000.0,
                        end_time_ns=time.time_ns(),
                    )
            raise

        # Capture the wall-clock end the instant dispatch returned - before
        # the request_finished receivers and instrumentation hooks run - so a
        # tracing bridge can anchor an accurate span window regardless of how
        # long a slow earlier hook/receiver takes.
        if instrument:
            end_time_ns = time.time_ns()
            duration_ms = (time.perf_counter() - started) * 1000.0

        # Signal: request finished. Sender is the app, `response=` is the
        # final Response, `request=` lets a receiver correlate with the
        # matching `request_started`. Receivers may peek but not replace.
        try:
            request_finished.send(self, response=response, request=request)
        except Exception:
            self.logger.exception("request_finished signal raised an exception")

        if instrument:
            # A HEAD response never iterates its body (the ASGI path sends
            # headers + an empty terminal frame), so its timing/status are
            # already final at this point - it is NOT a live stream even when
            # the underlying response object is a streaming type.
            is_streamed = response.is_streamed and request.method != HTTP_METHOD_HEAD
            await self._run_instrumentation(
                request,
                response.status_code,
                duration_ms,
                streamed=is_streamed,
                end_time_ns=end_time_ns,
            )

        return response

    async def _run_http_middleware_chain(self, request: Request) -> Response:
        """Run @app.middleware('http') functions with call_next pattern."""
        funcs = self._http_middleware_funcs

        def _make_next(level: int) -> Callable:
            _called = False

            async def call_next(req: Request) -> Response:
                nonlocal _called
                if _called:
                    raise RuntimeError("call_next() was called more than once")
                _called = True
                if level + 1 < len(funcs):
                    return await funcs[level + 1](req, _make_next(level + 1))
                return await self._dispatch_request(req)

            return call_next

        return await funcs[0](request, _make_next(0))

    async def _dispatch_request(self, request: Request) -> Response:
        """Core request dispatch - middleware, routing, handler execution.

        Thin orchestrator: the request phase, route resolution, handler
        invocation, and response hooks each live in a focused helper. The
        `try/finally` here owns the per-request teardown state (`_exc`,
        `_bp_name`, `resolver`) that the `finally` block reads.
        """
        _exc: Exception | None = None
        _bp_name: str | None = None
        # Resolver allocation is deferred until a non-trivial route demands
        # it. A trivial-plan route (no injected params, no dependencies)
        # never touches the resolver, so allocating one upfront - plus its
        # internal dict / WeakKeyDictionary / list members - would be pure
        # waste for the static-GET hot path. Per-request fresh allocation
        # is still preserved: a single shared resolver would let one
        # request's `reset()` clobber another's `yield`-teardown stack
        # (matches the per-connection resolver the WebSocket path uses).
        resolver: DependencyResolver | None = None
        try:
            # Run middleware (request phase)
            for mw in self._middlewares:
                early_response = await mw.process_request(request)
                if early_response is not None:
                    return await self._run_response_middleware(request, early_response)

            # Match the route once. `request.endpoint` is populated here so
            # before_request hooks can gate on the route name; the same
            # match object is reused for dispatch below.
            _matched_path = request.path
            _matched_method = request.method
            match = self.match(request.method, request.path)
            if match is not None:
                request.endpoint = match.route_info.name
                request._state["url_rule"] = match.route_info.path_template

            # Run before_request hooks (app-level then matched blueprint).
            # A non-None return short-circuits. `_bp_name` is recorded as the
            # matched blueprint so the `finally`-block teardown hooks fire for
            # the right blueprint even when dispatch short-circuits before the
            # final match is resolved.
            early, _bp_name = await self._run_before_hooks(request)
            if early is not None:
                return early

            # Resolve the route - handles mounted sub-apps, static files,
            # the re-match-after-hook-rewrite case, subdomain/host
            # constraints, slash redirects, and 404/405. Returns either a
            # terminal Response (already through response middleware) or the
            # match to dispatch.
            resolved = await self._resolve_route(request, match, _matched_path, _matched_method)
            if isinstance(resolved, Response):
                return resolved
            match = resolved
            _bp_name = _endpoint_blueprint(request.endpoint)

            # Resolve dependencies first and bind the resolver to this frame
            # *before* calling the handler - if the handler raises, the
            # `finally` block still sees the resolver and runs its
            # yield-dependency teardowns.
            kwargs, resolver = await self._resolve_dependencies(request, match)
            route_info = match.route_info
            result = await self._call_handler(
                route_info.handler,
                kwargs,
                is_coro=(
                    route_info.handler_plan.is_coro if route_info.handler_plan is not None else None
                ),
            )

            # Apply response_model, coerce, and merge any injected response.
            response = self._build_response(request, match, result)

            # Run after_request hooks (app + blueprint) and one-shot
            # `after_this_request` callbacks.
            response = await self._run_after_hooks(request, response, _bp_name)

            # Schedule any background tasks (DI-injected queue + the
            # response-attached task) in fire-and-forget fashion.
            self._schedule_background_tasks(request, response)

            # Inline empty-middleware gate skips the awaited no-op coroutine
            # creation in the common case (no middleware registered).
            if self._middlewares:
                response = await self._run_response_middleware(request, response)
            return response

        except HTTPException as exc:
            _exc = exc
            # Status-code handler wins over class handler; class handler walks
            # the MRO so e.g. registering on `HTTPException` catches `NotFound`.
            handler = self._status_handlers.get(exc.status_code) or self._find_exception_handler(
                type(exc)
            )
            if handler:
                result = await self._call_exc_handler(handler, request, exc)
                response = self._coerce_response(result)
                if self._middlewares:
                    response = await self._run_response_middleware(request, response)
                return response

            # `ValidationError` / `RequestValidationError` carry a
            # structured `.errors` list - emit it verbatim (the
            # shape `{"detail": [ {loc, msg, type}, ... ]}`) rather than
            # the stringified repr stored in `exc.detail`.
            structured = getattr(exc, "errors", None)
            detail_payload: Any = structured if structured is not None else exc.detail
            response = JSONResponse(
                {"detail": detail_payload, "status_code": exc.status_code},
                status_code=exc.status_code,
                headers=exc.headers,
            )
            if self._middlewares:
                response = await self._run_response_middleware(request, response)
            return response
        except Exception as exc:
            _exc = exc
            handler = self._find_exception_handler(type(exc))
            if handler:
                result = await self._call_exc_handler(handler, request, exc)
                response = self._coerce_response(result)
                if self._middlewares:
                    response = await self._run_response_middleware(request, response)
                return response

            # PROPAGATE_EXCEPTIONS: when set (or implicitly
            # when both DEBUG and TESTING are on), let the exception
            # escape the handler. Test suites use this to see real
            # tracebacks instead of "Internal Server Error" responses.
            if self._should_propagate_exceptions():
                raise

            if self.debug:
                # Serve the rich HTML traceback only to a client that prefers
                # HTML (a browser); curl / CLI / programmatic clients keep the
                # plain-text traceback they got before this page existed, so
                # the debug-mode Content-Type contract is unchanged for them.
                if _prefers_html(request):
                    body = render_traceback_html(exc).encode()
                    content_type = MIME_TEXT_HTML_UTF8
                else:
                    body = "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ).encode()
                    content_type = MIME_TEXT_PLAIN_UTF8
                response = Response(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    body=body,
                    content_type=content_type,
                )
                if self._middlewares:
                    response = await self._run_response_middleware(request, response)
                return response

            return await self._handle_error(
                request,
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                JSONResponse(
                    {"detail": MSG_INTERNAL_SERVER_ERROR},
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                ),
            )
        finally:
            # Yield-dependency teardowns first - they conceptually wrap the
            # request (the resource was acquired before the handler ran and
            # must be released regardless of outcome). Errors here are
            # swallowed inside `run_teardowns` so the response cycle stays
            # intact.
            # `run_teardowns` is async; the common no-yield-dep case has
            # an empty stack, so skip the coroutine + await entirely.
            if resolver is not None and resolver._teardowns:
                try:
                    await resolver.run_teardowns(_exc)
                except Exception:
                    self.logger.exception("yield-dependency teardown raised")

            # Teardown hooks - always run, even on exceptions.
            if self._teardown_request_hooks or self._bp_teardown_hooks:
                if (
                    self._bp_teardown_hooks
                    and _bp_name is not None
                    and _bp_name in self._bp_teardown_hooks
                ):
                    _td_hooks: list[Callable] = list(self._teardown_request_hooks)
                    _td_hooks.extend(self._bp_teardown_hooks[_bp_name])
                else:
                    _td_hooks = list(self._teardown_request_hooks)
            else:
                _td_hooks = ()  # type: ignore[assignment]
            if _td_hooks:
                await self._run_teardown_hooks(_td_hooks, _exc, "teardown_request")

            # `teardown_appcontext` fires when the app context pops; in
            # veloce that happens at the end of each request (no separate
            # app/request context split). Hooks receive the exception or
            # None. Errors are logged, never re-raised.
            if self._teardown_appcontext_hooks:
                await self._run_teardown_hooks(
                    self._teardown_appcontext_hooks, _exc, "teardown_appcontext"
                )

            # Signals: fire `got_request_exception` first when an exc bubbled
            # up, then always fire `request_tearing_down`. Receivers may
            # raise - log + continue so a buggy listener doesn't poison
            # the dispatch path. Names hoisted to module top.
            try:
                if _exc is not None:
                    got_request_exception.send(self, exception=_exc)
                request_tearing_down.send(self, exc=_exc)
            except Exception:
                self.logger.exception("signal receiver raised an exception")

    async def _run_before_hooks(self, request: Request) -> tuple[Response | None, str | None]:
        """Run before_request hooks; return `(short_circuit_response, bp_name)`.

        App-level hooks fire first, then the matched blueprint's (the
        blueprint bucket is selected from `request.endpoint` *after* the
        app-level hooks run, so a hook that rewrites the endpoint is
        honoured). A hook returning a non-None value short-circuits: it is
        coerced and passed through response middleware (unconditionally,
        matching the original early-return path) and returned.

        `bp_name` is the matched blueprint - `None` while the app-level hooks
        are still running, then the endpoint's blueprint once they complete.
        The orchestrator records it as the teardown blueprint, so a
        short-circuit inside an app-level hook leaves it `None` (no blueprint
        teardown) exactly as the inline version did.
        """
        for hook in self._before_request_hooks:
            result = await self._call_handler(hook, {"request": request})
            if result is not None:
                response = await self._run_response_middleware(
                    request, self._coerce_response(result)
                )
                return response, None
        bp_name = _endpoint_blueprint(request.endpoint)
        if self._bp_before_hooks and bp_name is not None:
            for hook in self._bp_before_hooks.get(bp_name, ()):
                result = await self._call_handler(hook, {"request": request})
                if result is not None:
                    response = await self._run_response_middleware(
                        request, self._coerce_response(result)
                    )
                    return response, bp_name
        return None, bp_name

    async def _resolve_route(
        self,
        request: Request,
        match: Any,
        matched_path: str,
        matched_method: str,
    ) -> Any:
        """Resolve the route to dispatch, or a terminal Response.

        Checks mounted sub-apps and static handlers first, re-matches when a
        before_request hook rewrote the path or method, enforces
        subdomain/host constraints, applies slash redirects, and produces the
        405/404 responses. Returns either a Response (already through response
        middleware) or the match to dispatch, having populated `path_params`,
        defaults, endpoint, and url_rule and run URL value preprocessors.
        Raises `HTTPException` for the 404 / constraint-mismatch cases.
        """
        # Check mounted sub-apps
        for prefix, prefix_slash, sub_app in self._mounted_apps:
            if request.path.startswith(prefix_slash) or request.path == prefix:
                sub_path = request.path[len(prefix) :] or "/"
                sub_request = Request(
                    method=request.method,
                    path=sub_path,
                    query_string=request.query_string,
                    headers=request.headers,
                    body=await request.body(),
                    transport=request.transport,
                    app=sub_app,
                )
                if hasattr(sub_app, "handle_request"):
                    response = await sub_app.handle_request(sub_request)
                    return await self._run_response_middleware(request, response)

        # Check static files
        for static in self._static_handlers:
            response = await static.handle(request)
            if response is not None:
                return await self._run_response_middleware(request, response)

        # Route matching - reuse the match taken before the before_request
        # hooks ran unless a hook rewrote the request path or method, in
        # which case the routing inputs changed and we must re-match.
        if request.path != matched_path or request.method != matched_method:
            match = self.match(request.method, request.path)

        # Subdomain constraint check - if the matched route declares a
        # `subdomain`, the request's host must be `{subdomain}.{SERVER_NAME}`.
        # Mismatch raises 404 directly (not 405, because
        # the path is reachable, just not from this host).
        # `subdomain="*"` accepts any non-empty subdomain.
        if (
            match is not None
            and match.route_info.subdomain is not None
            and not self._subdomain_matches(request, match.route_info.subdomain)
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_NOT_FOUND)

        # Host constraint check - the full `Host` header must equal
        # the route's declared `host` (case-insensitive, port-stripped).
        # Mismatch -> 404 (the path is reachable, just not from this host).
        if match is not None and match.route_info.host is not None:
            req_host = _extract_host(request.headers.get(HEADER_HOST, "") or "")
            if req_host != match.route_info.host:
                raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_NOT_FOUND)

        # Redirect slashes (like common web frameworks): /users -> /users/ or vice versa
        if match is None and self.redirect_slashes:
            alt = (
                request.path.rstrip("/")
                if request.path.endswith("/") and request.path != "/"
                else request.path + "/"
            )
            alt_match = self.match(request.method, alt)
            if alt_match is not None:
                code = (
                    status.HTTP_308_PERMANENT_REDIRECT
                    if request.method != HTTP_METHOD_GET
                    else status.HTTP_307_TEMPORARY_REDIRECT
                )
                response = RedirectResponse(alt, status_code=code)
                if self._middlewares:
                    response = await self._run_response_middleware(request, response)
                return response

        if match is None:
            # Check if path exists but method is wrong
            allowed = self.get_allowed_methods(request.path)
            if allowed:
                # RFC 9110 Sec. 9.3.7: OPTIONS auto-responds with `Allow:` and
                # an empty body even when no handler is registered.
                if request.method == HTTP_METHOD_OPTIONS:
                    response = self.make_default_options_response(
                        request.path, allowed_methods=allowed
                    )
                    if self._middlewares:
                        response = await self._run_response_middleware(request, response)
                    return response
                return await self._handle_error(
                    request,
                    status.HTTP_405_METHOD_NOT_ALLOWED,
                    JSONResponse(
                        {"detail": MSG_METHOD_NOT_ALLOWED, "allowed": allowed},
                        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
                        headers={HEADER_ALLOW: ", ".join(allowed)},
                    ),
                )
            raise HTTPException(status.HTTP_404_NOT_FOUND, MSG_NOT_FOUND)

        # Set path params + endpoint name on request.
        request.path_params = match.path_params
        # the routing-rule `defaults` - fill in fixed values for params
        # not already supplied by the matched URL.
        if match.route_info.defaults:
            for _dk, _dv in match.route_info.defaults.items():
                request.path_params.setdefault(_dk, _dv)
        request.endpoint = match.route_info.name
        request._state["url_rule"] = match.route_info.path_template

        # URL value preprocessors: mutate path_params in place
        # before the handler sees them. Endpoint is the route name.
        if self._url_value_preprocessors:
            endpoint = match.route_info.name
            for proc in self._url_value_preprocessors:
                proc(endpoint, request.path_params)

        return match

    async def _resolve_dependencies(
        self, request: Request, match: Any
    ) -> tuple[dict, DependencyResolver | None]:
        """Build the handler kwargs and the resolver that backs them.

        Returns `(kwargs, resolver)`. The resolver is `None` for trivial /
        request-only plans (no dependencies to resolve). The caller must
        hold the returned resolver so the dispatch `finally` can run its
        yield-dependency teardowns even when the handler raises.
        """
        # Fast path: consume the pre-built handler plan that Router.add_route
        # cached on RouteInfo at registration time.
        route_info = match.route_info
        if route_info.handler_plan is not None:
            if route_info.is_trivial_plan:
                return {}, None
            if route_info.is_request_only_plan:
                return {route_info.handler_plan.slots[0].name: request}, None
            resolver = DependencyResolver()
            resolver._overrides = self._dependency_overrides
            resolver._override_subplans = self._override_subplans
            kwargs = await resolver.resolve_plan(
                route_info.handler_plan,
                request,
                match.path_params,
                route_info.route_dep_plans,
            )
            return kwargs, resolver
        resolver = DependencyResolver()
        resolver._overrides = self._dependency_overrides
        resolver._override_subplans = self._override_subplans
        kwargs = await resolver.resolve(
            route_info.handler,
            request,
            match.path_params,
            route_dependencies=[d for d in route_info.dependencies if isinstance(d, Depends)],
        )
        return kwargs, resolver

    def _build_response(self, request: Request, match: Any, result: Any) -> Response:
        """Turn a handler return value into the final Response.

        Applies the route `response_model`, coerces to a Response, applies the
        route-level status_code override, and merges any handler-injected
        Response's status / headers.
        """
        route_info = match.route_info
        # Apply response_model validation + dump flags before coercion.
        # The handler may return a dict/BaseModel/list; if the route
        # declared a response_model, route the value through it so
        # extra fields drop, aliases apply, and unset/None filters fire.
        if route_info.response_model is not None and not isinstance(result, Response):
            result = self._apply_response_model(result, route_info)

        response = self._coerce_response(result, route_info.response_class)

        # Apply route-level status_code override
        if (
            route_info.status_code != status.HTTP_200_OK
            and response.status_code == status.HTTP_200_OK
        ):
            response.status_code = route_info.status_code
            response._encoded = None

        # Response injection - merge a handler-injected
        # Response's status_code + headers onto the final response.
        # Skipped when the handler returned a Response itself (its own
        # status/headers already win). `status_code == 0` means the
        # handler never touched it, so it is not applied.
        injected = request._state.get("_injected_response") if request._state else None
        if injected is not None and not isinstance(result, Response):
            if injected.status_code:
                response.status_code = injected.status_code
            for hk, hv in injected.headers.items():
                if hk.lower() == "set-cookie":
                    response._append_set_cookie_header(hv)
                else:
                    response.headers[hk] = hv
            response._encoded = None

        return response

    async def _run_after_hooks(
        self, request: Request, response: Response, bp_name: str | None
    ) -> Response:
        """Run after_request hooks and one-shot `after_this_request` callbacks.

        App-level hooks fire in reverse registration order, then the matched
        blueprint's, then the per-request one-shot callbacks. Each may return
        a replacement Response.
        """
        # Run after_request hooks - app-level then matched blueprint.
        for hook in reversed(self._after_request_hooks):
            hook_result = await self._call_handler(hook, {"request": request, "response": response})
            if hook_result is not None and isinstance(hook_result, Response):
                response = hook_result
        if self._bp_after_hooks and bp_name is not None:
            for hook in reversed(self._bp_after_hooks.get(bp_name, ())):
                hook_result = await self._call_handler(
                    hook, {"request": request, "response": response}
                )
                if hook_result is not None and isinstance(hook_result, Response):
                    response = hook_result

        # Drain one-shot `after_this_request(fn)` callbacks. These run
        # *after* the global hooks (so per-request adjustments see the
        # global hooks' mutations) and only for the current request.
        one_shot = request._state.get("_after_this_request") if request._state else None
        if one_shot:
            for fn in one_shot:
                fn_result = await self._call_handler(fn, {"request": request, "response": response})
                if fn_result is not None and isinstance(fn_result, Response):
                    response = fn_result
        return response

    def _schedule_background_tasks(self, request: Request, response: Response) -> None:
        """Schedule the DI-injected queue and response-attached background task.

        Both run fire-and-forget; a strong reference is held via the loop's
        task set and an error-logging done-callback is attached.
        """
        # Run background tasks if present - hold strong ref to prevent GC
        if request._background_tasks is not None:
            bg_task = asyncio.get_running_loop().create_task(request._background_tasks.run_all())
            bg_task.add_done_callback(self._log_background_task_error)

        # Response-attached background task (shape:
        # `Response(content=..., background=BackgroundTask(fn))`).
        # Runs in the same fire-and-forget pattern as the
        # DI-injected BackgroundTasks queue.
        attached_bg = getattr(response, "background", None)
        if attached_bg is not None:
            # `BackgroundTasks` collection -> `.run_all()`;
            # single `BackgroundTask` -> `.run()`. Anything else with
            # a `run()` coroutine method is supported too.
            if hasattr(attached_bg, "run_all"):
                coro = attached_bg.run_all()
            elif hasattr(attached_bg, "run"):
                coro = attached_bg.run()
            else:
                coro = None
            if coro is not None:
                bg_task = asyncio.get_running_loop().create_task(coro)
                bg_task.add_done_callback(self._log_background_task_error)

    async def _handle_error(
        self, request: Request, status_code: int, default: Response
    ) -> Response:
        """Check for status-code handler, fall back to default response."""
        handler = self._status_handlers.get(status_code)
        if handler:
            result = await self._call_handler(handler, {"request": request})
            return await self._run_response_middleware(request, self._coerce_response(result))
        return await self._run_response_middleware(request, default)

    def _subdomain_matches(self, request: Request, subdomain: str) -> bool:
        """Check whether `request`'s host carries the expected subdomain.

        `subdomain` is the literal subdomain string (`"api"`,
        `"admin"`) - the request's `Host` header must be
        `{subdomain}.{SERVER_NAME}`. `"*"` matches any non-empty
        subdomain of `SERVER_NAME`. When no `SERVER_NAME` is configured
        we degrade to comparing the leftmost label of the host with
        the subdomain literal - useful for tests that drive the app
        without setting `SERVER_NAME`.
        """
        host = _extract_host(request.host or "")
        if not host:
            return False
        server_name = (self.config.get("SERVER_NAME") or "").lower()
        if server_name:
            if not host.endswith("." + server_name):
                return False
            prefix = host[: -(len(server_name) + 1)]
            if subdomain == "*":
                return bool(prefix)
            return prefix == subdomain
        # No SERVER_NAME - compare the leftmost label.
        leftmost = host.split(".", 1)[0]
        if subdomain == "*":
            return "." in host
        return leftmost == subdomain

    async def _call_handler(
        self, handler: Callable, kwargs: dict, is_coro: bool | None = None
    ) -> Any:
        """Call a handler, supporting both sync and async.

        Sync handlers are offloaded to the default thread pool executor
        to prevent blocking the event loop. When the caller already knows
        whether the handler is a coroutine - the handler plan precomputes
        it at registration - it passes `is_coro` to skip the per-request
        `inspect.iscoroutinefunction` probe.
        """
        if is_coro is None:
            is_coro = _is_async_callable(handler)
        if is_coro:
            return await handler(**kwargs)
        # Run sync handlers in executor to avoid blocking the event loop.
        # Snapshot the current context so request-scoped `ContextVar`s
        # (`_current_request_var`, `_current_app_var`, `g`'s store)
        # remain readable in the worker thread - without `ctx.run`,
        # `loop.run_in_executor` runs the call in the executor's bare
        # context and helpers like `flash()`, `current_app.config[...]`,
        # and the `request` / `session` proxies all see "unbound". The
        # snapshot is read-only from the caller's perspective: a
        # `ContextVar.set(...)` inside the sync handler does not
        # propagate back.
        loop = asyncio.get_running_loop()
        ctx = contextvars.copy_context()
        return await loop.run_in_executor(None, ctx.run, functools.partial(handler, **kwargs))

    async def _call_exc_handler(
        self, handler: Callable, request: Request, exc: BaseException
    ) -> Any:
        """Call an exception handler, adapting kwargs to match its signature."""
        flags = _exc_handler_sig_cache.get(handler)
        if flags is None:
            params = set(inspect.signature(handler).parameters)
            flags = ("request" in params, "exc" in params)
            with contextlib.suppress(TypeError):
                _exc_handler_sig_cache[handler] = flags
        wants_request, wants_exc = flags
        kwargs: dict[str, Any] = {}
        if wants_request:
            kwargs["request"] = request
        if wants_exc:
            kwargs["exc"] = exc
        return await self._call_handler(handler, kwargs)

    def _apply_response_model(self, result: Any, route_info: Any) -> Any:
        """Route the handler return through `response_model` + dump flags.

        - Coerces dict / BaseModel / list-of-either through `model_validate`
          so undeclared fields drop.
        - Honours `response_model_exclude_unset`, `_exclude_defaults`,
          `_by_alias`, `_exclude_none`, `_include`, `_exclude` on `model_dump`.
        - For `list[Model]` declarations (e.g. response_model=list[User]),
          validates each element individually.
        """
        model = route_info.response_model
        dump_kwargs: dict[str, Any] = {}
        if route_info.response_model_exclude_unset:
            dump_kwargs["exclude_unset"] = True
        if route_info.response_model_exclude_defaults:
            dump_kwargs["exclude_defaults"] = True
        if route_info.response_model_by_alias:
            dump_kwargs["by_alias"] = True
        if route_info.response_model_exclude_none:
            dump_kwargs["exclude_none"] = True
        if route_info.response_model_include:
            dump_kwargs["include"] = route_info.response_model_include
        if route_info.response_model_exclude:
            dump_kwargs["exclude"] = route_info.response_model_exclude

        origin = get_origin(model)
        # Sequence-style response models - `response_model=list[Item]` - dump
        # each element through the inner model.
        if origin is list:
            args = get_args(model)
            if args:
                inner = args[0]
                if not isinstance(result, (list, tuple)):
                    return result  # let downstream coercion handle the mismatch
                if isinstance(inner, type) and issubclass(inner, _PydanticBaseModel):
                    dumped: list[Any] = []
                    for item in result:
                        # Fast path: an element already of the target model
                        # is dumped directly - skipping a re-validation
                        # round-trip and preserving the fields-set markers
                        # that `exclude_unset` reads (matching the scalar
                        # branch below).
                        if isinstance(item, inner):
                            dumped.append(item.model_dump(**dump_kwargs))
                        else:
                            dumped.append(inner.model_validate(item).model_dump(**dump_kwargs))
                    return dumped
            return result

        # Scalar Pydantic model.
        if isinstance(model, type) and issubclass(model, _PydanticBaseModel):
            # If the handler returned an instance of the target model, use
            # it directly - the dump-then-validate roundtrip would erase
            # the `__pydantic_fields_set__` info that drives
            # `exclude_unset`.
            if isinstance(result, model):
                return result.model_dump(**dump_kwargs)
            # Cross-model or dict input: dump any incoming BaseModel to a
            # dict first so model_validate can re-shape it. Cross-model
            # coercion (e.g. internal -> public view) works as expected;
            # `exclude_unset` semantics necessarily reset because the
            # fields-set markers don't transfer across model types.
            payload = result.model_dump() if isinstance(result, _PydanticBaseModel) else result
            validated = model.model_validate(payload)
            return validated.model_dump(**dump_kwargs)

        # Non-pydantic model (e.g. plain class) - pass through unchanged.
        return result

    def _log_background_task_error(self, task: asyncio.Task) -> None:
        """Done-callback for fire-and-forget background tasks.

        Pulls the exception off the future (silencing
        `Task exception was never retrieved` warnings) and logs it via
        `self.logger` so failures are observable instead of silently
        dropped. Never re-raises - the caller has already returned the
        response and there is nowhere meaningful for the error to go.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.logger.error("Background task failed", exc_info=exc)

    def _coerce_response(self, result: Any, response_class: Any = None) -> Response:
        """Convert handler return value to a Response object."""
        if isinstance(result, Response):
            return result
        # Use custom response_class if specified
        if response_class is not None:
            if isinstance(result, tuple):
                if len(result) == 3:
                    body, code, headers = result
                elif len(result) == 2:
                    body, second = result
                    if isinstance(second, int):
                        body, code, headers = body, second, {}
                    elif isinstance(second, dict):
                        body, code, headers = body, status.HTTP_200_OK, second
                    else:
                        body, code, headers = body, int(second), {}
                else:
                    body, code, headers = result[0], status.HTTP_200_OK, {}
                resp = self._coerce_response(body, response_class)
                resp.status_code = code
                resp.headers.update(headers)
                return resp
            if isinstance(response_class, type) and issubclass(response_class, JSONResponse):
                if isinstance(result, _PydanticBaseModel):
                    return response_class(result.model_dump())
                return response_class(result)
            if isinstance(result, str):
                return response_class(result)
            if isinstance(result, bytes):
                return response_class(result)
            return response_class(result)
        if isinstance(result, (dict, list)):
            return JSONResponse(result)
        if isinstance(result, str):
            # A bare `str` return defaults to text/html - the same default
            # `make_response()` applies, so the media type is consistent
            # whichever path produced the response.
            return Response(body=result.encode(), content_type=MIME_HTML)
        if isinstance(result, bytes):
            return Response(body=result, content_type=MIME_HTML)
        # Pydantic model
        if isinstance(result, _PydanticBaseModel):
            return JSONResponse(result.model_dump())
        # Tuple response (body, status_code) or (body, status_code, headers)
        if isinstance(result, tuple):
            if len(result) == 2:
                body, second = result
                if isinstance(second, int):
                    resp = self._coerce_response(body)
                    resp.status_code = second
                elif isinstance(second, dict):
                    resp = self._coerce_response(body)
                    resp.headers.update(second)
                else:
                    resp = self._coerce_response(body)
                    resp.status_code = int(second)
                resp._encoded = None
                return resp
            if len(result) == 3:
                body, code, headers = result
                resp = self._coerce_response(body)
                resp.status_code = code
                resp.headers.update(headers)
                resp._encoded = None
                return resp
        return JSONResponse(result)

    async def _run_response_middleware(self, request: Request, response: Response) -> Response:
        """Run middleware response phase in reverse order."""
        for mw in reversed(self._middlewares):
            response = await mw.process_response(request, response)
        return response

    async def _run_instrumentation(
        self,
        request: Request,
        status_code: int,
        duration_ms: float,
        streamed: bool = False,
        end_time_ns: int | None = None,
    ) -> None:
        """Deliver a `RequestMetrics` record to every instrumentation hook.

        A hook may be sync or async; one that raises is logged and skipped
        so observability code can never break the response.

        `streamed` marks responses whose body is emitted later on the ASGI
        send path; for those `duration_ms`/`status_code` cover only response
        production, not stream completion. See `RequestMetrics.streamed`.
        `end_time_ns` is the wall-clock end captured before any hook runs.
        """
        metrics = RequestMetrics(
            method=request.method,
            path=request.path,
            route=request.url_rule,
            status_code=status_code,
            duration_ms=duration_ms,
            streamed=streamed,
            end_time_ns=end_time_ns,
            # Inbound distributed-trace headers, carried verbatim so a tracing
            # bridge (e.g. veloce.otel) can extract a parent context and
            # continue the trace. Built on every dispatch path here - never via
            # a before_request hook, which a short-circuiting hook could skip.
            # `None` when the request carries no trace headers.
            parent_context=_trace_carrier(request),
        )
        for hook in self._instrumentation:
            try:
                result = hook(metrics)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                self.logger.exception("instrumentation hook raised an exception")

    # -- Server ---------------------------------------------------

    def _setup_openapi(self) -> None:
        """Register OpenAPI/Swagger routes if enabled."""
        if self._openapi_setup:
            return
        self._openapi_setup = True
        if self._openapi_url:
            from veloce.contrib.openapi import setup_openapi_routes

            # Pass the configured URLs through unchanged - `None` means
            # "do not register that UI", and must not be replaced by a
            # default path.
            setup_openapi_routes(
                self,
                openapi_url=self._openapi_url,
                docs_url=self._docs_url,
                redoc_url=self._redoc_url,
            )

    def openapi(self) -> dict[str, Any]:
        """Return the generated OpenAPI schema dict.

        Computes the schema on first call, caches the result in
        `app.openapi_schema`. Subsequent calls return the cached dict;
        users can mutate the result in place (e.g. to inject custom
        `info.x-logo` or `tags` orderings) and the swagger UI / json
        endpoints will serve the mutated copy.

        To bypass the auto-build entirely, assign a custom dict to
        `app.openapi_schema` before any request lands.
        """
        if self.openapi_schema is None:
            from veloce.contrib.openapi import get_openapi_schema

            self.openapi_schema = get_openapi_schema(self)
        return self.openapi_schema

    # Veloce exposes `openapi_version` and `openapi_schema` as direct
    # attributes for customisation. veloce stores `openapi_schema` on
    # the instance (None until first `openapi()` call); `openapi_version`
    # is the spec version string emitted in the document.
    openapi_version: str = "3.1.0"

    def run(
        self,
        host: str | None = None,
        port: int = 8000,
        workers: int = 1,
        access_log: bool = True,
        ssl_context: ssl.SSLContext | None = None,
        bind_all: bool = False,
    ) -> None:
        """Start the built-in **development** server.

        Veloce's from-scratch HTTP server is intended for local
        development only. For production, run the app under a hardened
        ASGI server - ``uvicorn your_module:app`` - which veloce is fully
        compatible with through its ASGI ``__call__`` interface.
        ``run()`` logs a reminder of this on startup.

        ``host`` resolves to ``"127.0.0.1"`` when unset so the dev server
        is reachable only from the local machine. Pass ``bind_all=True``
        to opt in to all-interfaces binding (``"0.0.0.0"``). ``host`` and
        ``bind_all=True`` are mutually exclusive - passing both raises
        ``ValueError`` to avoid silent privilege widening. Binding to
        ``0.0.0.0`` exposes the dev server to every reachable network -
        including remote attackers if the machine is on a public network
        - so it should be used only in trusted environments and never
        with ``debug=True``.

        ``ssl_context`` - an ``ssl.SSLContext`` - turns on HTTPS for local
        testing; it is handed straight to ``loop.create_server(ssl=...)``.
        Left ``None`` (the default) the serving path is byte-for-byte the
        same as plain HTTP. Production should still terminate TLS at
        uvicorn or a reverse proxy.
        """
        if host is not None and bind_all:
            raise ValueError(
                "Veloce.run: bind_all=True conflicts with explicit host=...; pass only one"
            )
        if host is None:
            host = "0.0.0.0" if bind_all else "127.0.0.1"
        self._setup_openapi()

        # The from-scratch server is dev-grade - make the production
        # recommendation impossible to miss.
        self.logger.warning(
            "veloce's built-in server (app.run()) is for local development only - "
            "run under uvicorn (or another hardened ASGI server) in production."
        )

        # Debug tracebacks leak source and internals - binding a non-local
        # host with debug=True exposes them to the network.
        if self.debug and host not in ("127.0.0.1", "::1", "localhost"):
            self.logger.warning(
                "debug=True with a non-local bind (host=%r) exposes debug "
                "tracebacks to the network - set debug=False for any deployment "
                "reachable beyond localhost.",
                host,
            )

        # Use uvloop if available (2-4x faster event loop)
        try:
            import uvloop

            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        except ImportError:
            pass

        if access_log:
            scheme = URL_SCHEME_HTTPS if ssl_context is not None else URL_SCHEME_HTTP
            print(f"\n  Veloce v{self.version}")
            print(f"  Listening on {scheme}://{host}:{port}")
            print("  Press Ctrl+C to stop\n")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._serve(host, port, access_log, ssl_context))
        except KeyboardInterrupt:
            pass
        finally:
            # Graceful shutdown: drain pending tasks, run lifecycle hooks
            loop.run_until_complete(self._graceful_shutdown(loop))
            loop.close()

    async def _serve(self, host: str, port: int, access_log: bool, ssl_context: Any = None) -> None:
        """Create the server and run forever."""
        # Deferred: serving.protocol imports `veloce.status`, which triggers
        # `veloce/__init__` -> back to this app module. Hoisting would
        # circle at package import time. Both call sites below share the
        # same break.
        from veloce.serving.protocol import HttpProtocol

        loop = asyncio.get_running_loop()
        # Run startup hooks
        await self._run_lifecycle(LIFECYCLE_STARTUP)

        # `ssl=None` (the default) makes `create_server` behave exactly as
        # the plain-HTTP path; TLS cost is paid only when a context is set.
        server = await loop.create_server(
            lambda: HttpProtocol(self, loop),
            host,
            port,
            reuse_port=True,
            ssl=ssl_context,
        )
        # Handle signals for graceful shutdown
        shutdown_event = asyncio.Event()

        def _signal_handler() -> None:
            server.close()
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _signal_handler)

        async with server:
            await shutdown_event.wait()

    async def _graceful_shutdown(self, loop: asyncio.AbstractEventLoop) -> None:
        """Drain in-flight requests and run shutdown lifecycle."""
        # Deferred: same `veloce.status` -> `veloce/__init__` cycle that
        # the matching import in `_serve` breaks. These are the only two
        # call sites; not worth a structural refactor.
        from veloce.serving.protocol import HttpProtocol

        # Wait for active dispatch tasks to complete (with timeout)
        if HttpProtocol._active_tasks:
            await asyncio.wait(
                HttpProtocol._active_tasks,
                timeout=30,
            )

        # Cancel any still-running tasks
        for task in HttpProtocol._active_tasks:
            task.cancel()
        HttpProtocol._active_tasks.clear()

        # Run shutdown lifecycle hooks
        await self._run_lifecycle(LIFECYCLE_SHUTDOWN)

    async def _run_lifecycle(self, event: str) -> None:
        """Run lifecycle event handlers, including lifespan context manager."""
        if event == LIFECYCLE_STARTUP:
            # Lifespan context manager
            if self._lifespan is not None:
                self._lifespan_cm = self._lifespan(self)
                await self._lifespan_cm.__aenter__()

            for handler in self._on_startup:
                if _is_async_callable(handler):
                    await handler()
                else:
                    loop = asyncio.get_running_loop()
                    ctx = contextvars.copy_context()
                    await loop.run_in_executor(None, ctx.run, functools.partial(handler))

            # Dev-mode event-loop blocking watchdog - opt-in, so an app
            # that does not set the config key never builds one. The key
            # may be a plain truthy value, or a mapping of watchdog kwargs
            # (`interval`, `stall_threshold`) for tuning.
            _wd_config = self.config.get("EVENT_LOOP_WATCHDOG")
            if _wd_config and self._watchdog is None:
                from veloce.watchdog import EventLoopWatchdog

                _wd_kwargs = dict(_wd_config) if isinstance(_wd_config, Mapping) else {}
                self._watchdog = EventLoopWatchdog(asyncio.get_running_loop(), **_wd_kwargs)
                self._watchdog.start()
        else:
            if self._watchdog is not None:
                self._watchdog.stop()
                self._watchdog = None

            for handler in self._on_shutdown:
                if _is_async_callable(handler):
                    await handler()
                else:
                    loop = asyncio.get_running_loop()
                    ctx = contextvars.copy_context()
                    await loop.run_in_executor(None, ctx.run, functools.partial(handler))

            # Exit lifespan context manager
            if self._lifespan_cm is not None:
                await self._lifespan_cm.__aexit__(None, None, None)
                self._lifespan_cm = None

    def lifespan_context(self) -> _LifespanManager:
        """Return an async context manager driving the lifespan cycle.

        `async with app.lifespan_context(): ...` runs the full startup
        sequence (lifespan CM enter + `on_startup` handlers) on entry
        and the shutdown sequence on exit - independent of any request.
        Useful for tests and for embedding the app where you want
        startup/shutdown without an ASGI server in the loop.
        """
        return _LifespanManager(self)

    # -- ASGI compatibility layer ---------------------------------

    async def _emit_413(self, send: Callable, limit: int) -> None:
        """Emit a 413 response directly over ASGI.

        Used by the incremental body-size guard in `__call__`, which
        runs before a `Request` object exists.
        """
        resp = JSONResponse(
            {
                "detail": MSG_REQUEST_BODY_EXCEEDS_MAX,
                "status_code": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "limit": limit,
            },
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        )
        body = resp.body
        await send(
            {
                "type": ASGI_EVENT_HTTP_RESPONSE_START,
                "status": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "headers": [
                    (RAW_HEADER_CONTENT_TYPE, resp.content_type.encode()),
                    (RAW_HEADER_CONTENT_LENGTH, str(len(body)).encode()),
                ],
            }
        )
        await send({"type": ASGI_EVENT_HTTP_RESPONSE_BODY, "body": body})

    def _build_asgi_stack(self) -> Callable:
        """Wrap the core ASGI app with each registered ASGI middleware.

        The first-registered middleware ends up the outermost wrapper, so
        it sees the request first and the response last.
        """
        app: Callable = self._asgi_app
        for cls, options in reversed(self._asgi_middleware):
            app = cls(app, **options)
        return app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        """ASGI interface - allows running under uvicorn/hypercorn if desired.

        Any third-party ASGI middleware registered via `add_middleware` wraps
        the core application here; with none registered this is a direct call
        to `_asgi_app` with no measurable overhead.
        """
        if self._asgi_middleware:
            stack = self._asgi_stack
            if stack is None:
                stack = self._build_asgi_stack()
                self._asgi_stack = stack
            await stack(scope, receive, send)
        else:
            await self._asgi_app(scope, receive, send)

    async def _asgi_app(self, scope: dict, receive: Callable, send: Callable) -> None:
        """The core ASGI application - HTTP / WebSocket / lifespan handling."""
        if not self._openapi_setup:
            self._setup_openapi()

        # Mounted arbitrary ASGI apps are dispatched here with the raw
        # scope - the matched prefix is moved from `path` to `root_path`.
        if self._asgi_mounts and scope["type"] in (ASGI_SCOPE_HTTP, ASGI_SCOPE_WEBSOCKET):
            mount = self._match_asgi_mount(scope.get("path", ""))
            if mount is not None:
                prefix, mounted = mount
                sub_scope = dict(scope)
                sub_scope["path"] = scope["path"][len(prefix) :] or "/"
                sub_scope["root_path"] = scope.get("root_path", "") + prefix
                # Drop the now-stale absolute `raw_path`; the mounted app
                # falls back to the rewritten `path`.
                sub_scope.pop("raw_path", None)
                await mounted(sub_scope, receive, send)
                return

        if scope["type"] == ASGI_SCOPE_HTTP:
            # Hand the raw ASGI `(bytes, bytes)` header list to `Request`
            # untouched; the CIMultiDict + per-tuple latin-1 decode is
            # deferred until the handler reads `request.headers`. The
            # hot json-hello / path-param path never reads them.
            raw_headers = scope.get("headers", [])

            # MAX_CONTENT_LENGTH: declared value refused up front; the
            # running total catches chunked bodies that omit it. The check
            # walks raw bytes tuples rather than forcing the Headers build.
            max_size = self.config.get("MAX_CONTENT_LENGTH")
            if max_size is not None:
                declared_b: bytes | None = None
                # ASGI mandates lowercase header names, but `.lower()`
                # defends against a non-compliant server before we trust
                # the declared length. The loop only runs when
                # `MAX_CONTENT_LENGTH` is configured (cold on the hot path).
                for _hk, _hv in raw_headers:
                    if _hk.lower() == RAW_HEADER_CONTENT_LENGTH:
                        declared_b = _hv
                        break
                if declared_b is not None:
                    try:
                        over = int(declared_b) > max_size
                    except ValueError:
                        over = False
                    if over:
                        await self._emit_413(send, max_size)
                        return

            # Common case - one body chunk, no `more_body`. Skip the
            # body_parts list + join.
            message = await receive()
            body = message.get("body", b"") or b""
            if message.get("more_body", False):
                body_parts = [body] if body else []
                received = len(body)
                while True:
                    message = await receive()
                    chunk = message.get("body", b"")
                    if chunk:
                        body_parts.append(chunk)
                        received += len(chunk)
                        if max_size is not None and received > max_size:
                            await self._emit_413(send, max_size)
                            return
                    if not message.get("more_body", False):
                        break
                body = b"".join(body_parts)
            elif max_size is not None and len(body) > max_size:
                await self._emit_413(send, max_size)
                return

            # ASGI HTTP scope mandates `path` and `query_string` keys -
            # direct subscript skips the `.get(default)` default-arg pop.
            path = scope["path"]
            query = scope["query_string"].decode("ascii")

            request = Request(
                method=scope["method"],
                path=path,
                query_string=query,
                headers=raw_headers,
                body=body,
                scope=scope,
            )

            response = await self.handle_request(request)

            # Streaming response - emit the body as a sequence of ASGI
            # `http.response.body` chunks instead of one buffered
            # payload. No `content-length`: the ASGI server frames it.
            if response.is_streamed:
                # CRLF-validate every header value - the ASGI emit path
                # bypasses `Response.encode()`, so the splitting guard must
                # be applied here too. Built-in content types hit the cache.
                _ct = response.content_type
                _ct_bytes = _CT_BYTES_CACHE.get(_ct)
                if _ct_bytes is None:
                    _ct_bytes = _reject_header_crlf(_ct, "content-type").encode()
                stream_headers: list[tuple[bytes, bytes]] = [
                    (RAW_HEADER_CONTENT_TYPE, _ct_bytes),
                ]
                for sk, sv in response.headers.items():
                    sk_lower = sk.lower()
                    if sk_lower == "content-length":
                        continue
                    if sk_lower == "set-cookie":
                        for piece in sv.split("\r\nSet-Cookie:"):
                            cookie = piece.strip()
                            _reject_header_crlf(cookie, MSG_LABEL_SET_COOKIE_VALUE)
                            stream_headers.append(
                                (
                                    RAW_HEADER_SET_COOKIE,
                                    _encode_header_value(cookie).encode("latin-1"),
                                )
                            )
                    else:
                        _reject_header_crlf(sk, MSG_LABEL_HEADER_NAME)
                        _reject_header_crlf(sv, f"{sk} header value")
                        stream_headers.append(
                            (sk_lower.encode(), _encode_header_value(sv).encode("latin-1"))
                        )
                await send(
                    {
                        "type": ASGI_EVENT_HTTP_RESPONSE_START,
                        "status": response.status_code,
                        "headers": stream_headers,
                    }
                )
                if scope["method"] != HTTP_METHOD_HEAD:
                    async for chunk in getattr(response, "_stream"):  # noqa: B009
                        await send(
                            {
                                "type": ASGI_EVENT_HTTP_RESPONSE_BODY,
                                "body": chunk.encode("utf-8") if isinstance(chunk, str) else chunk,
                                "more_body": True,
                            }
                        )
                await send({"type": ASGI_EVENT_HTTP_RESPONSE_BODY, "body": b"", "more_body": False})
                return

            # Bodiless statuses (1xx interim, 204, 205, 304) MUST NOT carry a
            # payload (RFC 9110 Sec. 15.2 / 15.3.5 / 15.3.6 / 15.4.5). Strip the
            # body before sending and, below, suppress the framework-default
            # content-type so a `JSONResponse(204)` does not advertise
            # `application/json` over zero bytes.
            body_allowed = status.status_permits_body(response.status_code)
            # A 304 (like HEAD) may carry the would-be-200 Content-Length while
            # sending no body (RFC 9110 Sec. 8.6 / 15.4.5); 1xx/204/205 have no
            # representation, so their length is 0.
            is_304 = response.status_code == status.HTTP_304_NOT_MODIFIED
            advertised_length = len(response.body) if (body_allowed or is_304) else 0
            body_out = response.body if body_allowed else b""

            # RFC 9110 Sec. 9.3.2: HEAD responses must not include a payload
            # body, but `Content-Length` (and other content-related headers)
            # should still reflect the size the equivalent GET would have
            # produced. Blank the body but keep the advertised length, same as
            # the 304 case above.
            head_content_length: int | None = None
            if scope["method"] == HTTP_METHOD_HEAD or is_304:
                head_content_length = advertised_length
                body_out = b""

            # Build the ASGI header list. Each header MUST be its own
            # `(name, value)` tuple; multiple cookies (`Set-Cookie`) get one
            # tuple each. `Response.set_cookie` joins multiple cookies into
            # one header value with `\r\nSet-Cookie: ` literal for the raw
            # HTTP/1.1 wire path; split that back into per-cookie tuples
            # here so the ASGI contract is honoured.
            content_length = (
                head_content_length if head_content_length is not None else len(body_out)
            )
            # CRLF-validate every header value - the ASGI emit path
            # bypasses `Response.encode()`, so the response-splitting
            # guard must be applied here too. Built-in content types and
            # small content-length values hit the precomputed caches.
            _ct = response.content_type
            _ct_bytes = _CT_BYTES_CACHE.get(_ct)
            if _ct_bytes is None:
                _ct_bytes = _reject_header_crlf(_ct, "content-type").encode()
            _cl_bytes = (
                _CL_BYTES_SMALL[content_length]
                if 0 <= content_length < 2048
                else str(content_length).encode("ascii")
            )
            # Single pass over the response headers: emit each as an ASGI
            # tuple while tracking whether a content-type / content-length
            # was supplied, so the framework default is only prepended when
            # the response does not already carry it.
            has_ct = False
            has_cl = False
            asgi_headers: list[tuple[bytes, bytes]] = []
            if response.headers:
                for k, v in response.headers.items():
                    k_lower = k.lower()
                    if k_lower == "set-cookie":
                        # `Response.set_cookie` joins multiple cookies into one
                        # header value with `\r\nSet-Cookie: ` literal for the
                        # raw HTTP/1.1 wire path. Split it back into per-cookie
                        # ASGI tuples regardless of how many cookies are there.
                        for piece in v.split("\r\nSet-Cookie:"):
                            cookie = piece.strip()
                            _reject_header_crlf(cookie, MSG_LABEL_SET_COOKIE_VALUE)
                            asgi_headers.append(
                                (
                                    RAW_HEADER_SET_COOKIE,
                                    _encode_header_value(cookie).encode("latin-1"),
                                )
                            )
                    else:
                        if k_lower == "content-type":
                            has_ct = True
                        elif k_lower == "content-length":
                            has_cl = True
                        _reject_header_crlf(k, MSG_LABEL_HEADER_NAME)
                        _reject_header_crlf(v, f"{k} header value")
                        asgi_headers.append(
                            (k_lower.encode(), _encode_header_value(v).encode("latin-1"))
                        )
            # Prepend the framework default content-type/content-length only
            # when the response does not already carry that header. A user or
            # middleware value (e.g. the compressed length from
            # `GZipMiddleware`) was emitted above and wins; prepending the
            # default too would put a duplicate header on the wire.
            if not has_cl:
                asgi_headers.insert(0, (RAW_HEADER_CONTENT_LENGTH, _cl_bytes))
            # Never default a content-type onto a bodiless response (an explicit
            # handler-set content-type still survives via has_ct).
            if not has_ct and body_allowed:
                asgi_headers.insert(0, (RAW_HEADER_CONTENT_TYPE, _ct_bytes))

            await send(
                {
                    "type": ASGI_EVENT_HTTP_RESPONSE_START,
                    "status": response.status_code,
                    "headers": asgi_headers,
                }
            )
            await send(
                {
                    "type": ASGI_EVENT_HTTP_RESPONSE_BODY,
                    "body": body_out,
                }
            )

        elif scope["type"] == ASGI_SCOPE_WEBSOCKET:
            # ASGI WS dispatch (W1). Match the route table for a
            # WEBSOCKET-method handler and run it with a WebSocket built
            # from the ASGI receive/send pair. Path params are coerced
            # the same way they are for HTTP.
            _current_app_var.set(self)
            g._reset()

            # Host and Origin validation for WebSocket handshakes - an HTTP
            # middleware such as TrustedHostMiddleware or
            # WebSocketOriginMiddleware never sees a `websocket` scope, so
            # apply any host allow-list and Origin allow-list directly here.
            ws_host = ""
            ws_origin = ""
            _host_seen = False
            _origin_seen = False
            for _hk, _hv in scope.get("headers", []):
                # First occurrence of each header wins - a duplicate
                # `Origin` must not be able to shadow the real one.
                if _hk == b"host" and not _host_seen:
                    ws_host = _extract_host(_hv.decode("latin-1"))
                    _host_seen = True
                elif _hk == b"origin" and not _origin_seen:
                    ws_origin = _hv.decode("latin-1")
                    _origin_seen = True
            for _mw in self._middlewares:
                _host_check = getattr(_mw, "is_host_allowed", None)
                if _host_check is not None and not _host_check(ws_host):
                    msg = await receive()
                    if msg["type"] == ASGI_EVENT_WS_CONNECT:
                        await send(
                            {"type": ASGI_EVENT_WS_CLOSE, "code": status.WS_1008_POLICY_VIOLATION}
                        )
                    return
                _origin_check = getattr(_mw, "is_websocket_origin_allowed", None)
                if _origin_check is not None and not _origin_check(ws_origin):
                    msg = await receive()
                    if msg["type"] == ASGI_EVENT_WS_CONNECT:
                        await send(
                            {"type": ASGI_EVENT_WS_CLOSE, "code": status.WS_1008_POLICY_VIOLATION}
                        )
                    return

            ws_match = self.match(ROUTE_METHOD_WEBSOCKET, scope.get("path", "/"))
            if ws_match is None:
                # No handler - refuse the connection per ASGI WS spec.
                msg = await receive()
                if msg["type"] == ASGI_EVENT_WS_CONNECT:
                    await send(
                        {"type": ASGI_EVENT_WS_CLOSE, "code": status.WS_1008_POLICY_VIOLATION}
                    )
                return

            ws = WebSocket.from_asgi(scope, receive, send)
            ws.path_params = ws_match.path_params
            route_info = ws_match.route_info
            # A fresh resolver per connection: a WebSocket is long-lived,
            # so its yield-dependency teardown stack must not be cleared
            # by a concurrent request resetting the shared HTTP resolver.
            ws_resolver = DependencyResolver()
            ws_resolver._overrides = self._dependency_overrides
            ws_resolver._override_subplans = self._override_subplans
            ws_exc: BaseException | None = None
            try:
                handler = route_info.handler
                # WebSocket DI runs through the shared HandlerPlan /
                # DependencyResolver - the same path as HTTP dispatch - so
                # WebSocket dependencies get `yield`-style teardown and
                # `Security` / `SecurityScopes` support (F8).
                if route_info.handler_plan is not None:
                    try:
                        kwargs = await ws_resolver.resolve_ws_plan(
                            route_info.handler_plan,
                            ws,
                            ws_match.path_params,
                            route_info.route_dep_plans,
                        )
                    except RequestValidationError as exc:
                        # A WebSocket dependency failed validation -
                        # surface it as the WS-specific error (V9).
                        raise WebSocketRequestValidationError(
                            getattr(exc, "errors", []) or []
                        ) from exc
                else:
                    kwargs = {}
                await handler(**kwargs)
            except WebSocketRequestValidationError:
                # Dependency validation failure - close with 1008
                # (policy violation), not 1011, and swallow.
                if not ws._closed:
                    with contextlib.suppress(Exception):
                        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
            except WebSocketException as exc:
                # Application-driven close - send the requested code +
                # reason and swallow the exception (not an error).
                if not ws._closed:
                    with contextlib.suppress(Exception):
                        await ws.close(code=exc.code, reason=exc.reason or "")
            except Exception as exc:
                ws_exc = exc
                if not ws._closed:
                    with contextlib.suppress(Exception):
                        await ws.close(code=status.WS_1011_INTERNAL_ERROR)  # internal error
                raise
            else:
                if not ws._closed:
                    with contextlib.suppress(Exception):
                        await ws.close()
            finally:
                # Drain any `yield`-style dependency teardowns the
                # handshake set up, exception-aware.
                await ws_resolver.run_teardowns(ws_exc)

        elif scope["type"] == ASGI_SCOPE_LIFESPAN:
            while True:
                message = await receive()
                if message["type"] == ASGI_EVENT_LIFESPAN_STARTUP:
                    try:
                        await self._run_lifecycle(LIFECYCLE_STARTUP)
                        await send({"type": ASGI_EVENT_LIFESPAN_STARTUP_COMPLETE})
                    except Exception as exc:
                        await send(
                            {"type": ASGI_EVENT_LIFESPAN_STARTUP_FAILED, "message": str(exc)}
                        )
                        return
                elif message["type"] == ASGI_EVENT_LIFESPAN_SHUTDOWN:
                    await self._run_lifecycle(LIFECYCLE_SHUTDOWN)
                    await send({"type": ASGI_EVENT_LIFESPAN_SHUTDOWN_COMPLETE})
                    return
