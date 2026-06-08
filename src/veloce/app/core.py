"""Veloce application — the main entry point."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import functools
import os
import sys
import warnings
import weakref
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from veloce._internal import (
    MIME_HTML,
    _coerce_bool,
    _is_async_callable,
    offload,
)
from veloce._pipeline import (
    PH_ASGI_WRAP,
    PH_HTTP_AROUND,
    PH_HTTP_FINISH,
    PH_HTTP_POST,
    PH_HTTP_PRE,
    PH_WS_HANDSHAKE,
    CompiledPipeline,
    FeatureSpec,
    build_request_middleware,
    build_response_middleware,
    build_ws_handshake_checks,
    compile_pipeline,
)
from veloce._protocol_constants import (
    HTTP_METHOD_GET,
    LIFECYCLE_SHUTDOWN,
    LIFECYCLE_STARTUP,
)
from veloce.app.asgi import AsgiMixin
from veloce.app.background import BackgroundTasksMixin
from veloce.app.dispatch import DispatchMixin
from veloce.app.errors import ErrorsMixin
from veloce.app.serving import ServingMixin
from veloce.app.templating import TemplatingMixin
from veloce.app.urls import URLRule as URLRule
from veloce.app.urls import _URLMap
from veloce.blueprints import _endpoint_blueprint
from veloce.contrib.staticfiles import StaticFiles
from veloce.exceptions import (
    SetupError,
)
from veloce.helpers import g
from veloce.http.datastructures import State
from veloce.http.request import Request
from veloce.http.response import (
    Response,
)
from veloce.middleware import BaseHTTPMiddleware, Middleware
from veloce.routing.router import Router, _readd_route

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app.contexts import _AppContext, _LifespanManager, _TestRequestContext


# Sentinel for cache misses where `None` is itself a valid cache hit
# (e.g. "no exception handler matched this type"). Plain `cache.get(k)`
# would re-walk the MRO every time for an unhandled exception type.
_MISSING: Any = object()

# `BaseExceptionGroup` is a builtin only from Python 3.11; on 3.10 it is `None`
# so the unwind helpers below fall back to single-exception chaining.
_BaseExceptionGroup: type[BaseException] | None = getattr(builtins, "BaseExceptionGroup", None)


def _collect_chained(exc: BaseException) -> list[BaseException]:
    """Flatten an exception and its `__context__` chain into a list.

    `AsyncExitStack.aclose()` runs every teardown, chaining each failure onto
    the previous through `__context__` and re-raising the last. Walking that
    chain recovers all teardown failures (oldest last), and an interior
    `BaseExceptionGroup` is expanded so its members are surfaced individually.
    A cycle guard keeps the walk bounded even on a self-referential chain.
    """
    out: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _BaseExceptionGroup is not None and isinstance(current, _BaseExceptionGroup):
            out.extend(current.exceptions)  # type: ignore[attr-defined]
        else:
            out.append(current)
        current = current.__context__
    # Reverse so the first teardown that failed leads the group, matching the
    # order the teardowns ran.
    out.reverse()
    return out


def _raise_unwind_errors(errors: list[BaseException]) -> None:
    """Re-raise lifespan-unwind failures, grouping them when possible.

    A single failure is re-raised as-is so its traceback is preserved
    verbatim. Several failures are combined into a `BaseExceptionGroup`
    (Python 3.11+) so none is masked; on 3.10, where groups are unavailable,
    the first failure is raised with the rest chained as a note.
    """
    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    if _BaseExceptionGroup is not None:
        raise _BaseExceptionGroup("lifespan shutdown failed", errors)
    first = errors[0]
    for extra in errors[1:]:
        with contextlib.suppress(Exception):
            first.add_note(  # type: ignore[attr-defined]
                f"+ also raised during lifespan unwind: {extra!r}"
            )
    raise first


class Veloce(
    AsgiMixin,
    DispatchMixin,
    ErrorsMixin,
    ServingMixin,
    BackgroundTasksMixin,
    TemplatingMixin,
    Router,
):
    """Ultra-fast async web framework.

    Usage::

        app = Veloce()

        @app.get("/")
        async def index(request: Request):
            return {"message": "Hello, World!"}

        app.run()
    """

    # Veloce exposes `openapi_version` and `openapi_schema` as direct
    # attributes for customisation. veloce stores `openapi_schema` on
    # the instance (None until first `openapi()` call); `openapi_version`
    # is the spec version string emitted in the document.
    openapi_version: str = "3.1.0"

    def __init__(
        self,
        title: Annotated[
            str,
            Doc("API title shown in the OpenAPI document and the docs UI."),
        ] = "Veloce",
        version: Annotated[
            str,
            Doc("API version string emitted into the OpenAPI document."),
        ] = "0.1.0",
        description: Annotated[
            str,
            Doc("Longer API description emitted into the OpenAPI document."),
        ] = "",
        summary: Annotated[
            str | None,
            Doc("Short one-line API summary emitted into the OpenAPI document."),
        ] = None,
        debug: Annotated[
            bool,
            Doc("Enable debug mode: verbose error pages and development conveniences."),
        ] = False,
        prefix: Annotated[
            str,
            Doc("Path prefix prepended to every route registered on the app."),
        ] = "",
        docs_url: Annotated[
            str | None,
            Doc("Path serving the Swagger UI docs page; `None` disables it."),
        ] = "/docs",
        redoc_url: Annotated[
            str | None,
            Doc("Path serving the ReDoc docs page; `None` disables it."),
        ] = "/redoc",
        openapi_url: Annotated[
            str | None,
            Doc("Path serving the generated OpenAPI JSON document; `None` disables it."),
        ] = "/openapi.json",
        lifespan: Annotated[
            Callable | None,
            Doc("Async context manager managing startup and shutdown resources for the app."),
        ] = None,
        redirect_slashes: Annotated[
            bool,
            Doc(
                "Redirect a request to the canonical slashed/unslashed form on a trailing-slash mismatch."
            ),
        ] = True,
        root_path: Annotated[
            str,
            Doc("ASGI root path the app is mounted under, used for URL generation behind a proxy."),
        ] = "",
        openapi_tags: Annotated[
            list[dict[str, Any]] | None,
            Doc("OpenAPI tag metadata objects describing and ordering the document's tags."),
        ] = None,
        openapi_external_docs: Annotated[
            dict[str, Any] | None,
            Doc("OpenAPI external-documentation object for the document root."),
        ] = None,
        servers: Annotated[
            list[dict[str, Any]] | None,
            Doc("OpenAPI server objects listing the base URLs the API is served from."),
        ] = None,
        license_info: Annotated[
            dict[str, str] | None,
            Doc("OpenAPI license object for the API."),
        ] = None,
        contact: Annotated[
            dict[str, str] | None,
            Doc("OpenAPI contact object for the API."),
        ] = None,
        terms_of_service: Annotated[
            str | None,
            Doc("URL to the API's terms of service, emitted into the OpenAPI document."),
        ] = None,
        swagger_ui_parameters: Annotated[
            dict[str, Any] | None,
            Doc("Configuration parameters passed to the Swagger UI docs page."),
        ] = None,
        swagger_ui_init_oauth: Annotated[
            dict[str, Any] | None,
            Doc("OAuth2 initialization config passed to Swagger UI's `initOAuth`."),
        ] = None,
        separate_input_output_schemas: Annotated[
            bool,
            Doc(
                "Emit a distinct serialization schema for a model whose input and output schemas diverge."
            ),
        ] = True,
        disambiguate_operation_ids: Annotated[
            bool,
            Doc(
                "Deterministically suffix colliding auto-generated operationIds so the document stays valid."
            ),
        ] = True,
        validate_openapi: Annotated[
            bool | None,
            Doc("Run the structural OpenAPI checker after assembly; `None` defers to `debug`."),
        ] = None,
        default_response_class: Annotated[
            Any,
            Doc("Response class used for routes that do not declare their own `response_class`."),
        ] = None,
        dependencies: Annotated[
            list[Any] | None,
            Doc("Dependencies applied to every route on the app, run before per-route ones."),
        ] = None,
        responses: Annotated[
            dict[int, dict[str, Any]] | None,
            Doc("Additional OpenAPI responses overlaid onto every route on the app."),
        ] = None,
        exception_handlers: Annotated[
            dict[Any, Callable] | None,
            Doc(
                "Mapping of exception class or status code to a handler, registered at construction."
            ),
        ] = None,
        middleware: Annotated[
            list[Any] | None,
            Doc("Middleware instances registered on the app at construction, outermost first."),
        ] = None,
        import_name: Annotated[
            str | None,
            Doc(
                "Import name used to locate templates and static files; defaults to the caller's module."
            ),
        ] = None,
        template_folder: Annotated[
            str | None,
            Doc("Directory holding templates, resolved relative to the application root path."),
        ] = None,
        instance_path: Annotated[
            str | None,
            Doc("Instance folder for runtime files; computed from the package root when omitted."),
        ] = None,
        on_duplicate: Annotated[
            str,
            Doc(
                "Policy for a second handler on the same path and method: `error`, `warn`, or `override`."
            ),
        ] = "error",
        **extra: Any,
    ) -> None:
        # App-level `dependencies` / `responses` - applied
        # to every route registered on the app (per-route entries are
        # appended / overlaid on top). `on_duplicate` controls what happens
        # when two handlers claim the same path+method (default: raise).
        super().__init__(
            prefix=prefix,
            default_response_class=default_response_class,
            dependencies=dependencies,
            responses=responses,
            on_duplicate=on_duplicate,
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
        # OpenAPI generation knobs (consumed by veloce.contrib.openapi):
        # - `separate_input_output_schemas`: emit a distinct serialization
        #   (`-Output`) schema for a model whose validation and serialization
        #   JSON Schemas diverge (computed/write-only fields). When False the
        #   validation schema is reused for both request and response.
        # - `disambiguate_operation_ids`: deterministically suffix colliding
        #   auto-generated operationIds so the document stays codegen-valid.
        # - `validate_openapi`: run the lightweight structural checker after
        #   the document is assembled; `None` defers to `app.debug`.
        self.separate_input_output_schemas = separate_input_output_schemas
        self.disambiguate_operation_ids = disambiguate_operation_ids
        self.validate_openapi = validate_openapi

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
        # Setup lock: flipped True on the first dispatch (under
        # `_first_request_lock`) so late route/hook/blueprint registration -
        # which would race in-flight requests under concurrent ASGI dispatch -
        # raises `SetupError` instead of silently mutating the live route table.
        # Initialised here, before the ctor-time `exception_handlers=` /
        # `middleware=` registration runs, so `_assert_mutable` can read it.
        # Relaxed under DEBUG/TESTING (decided at lock time) so hot-reload and
        # test monkeypatching stay ergonomic.
        self._setup_locked = False
        # Master switch for the setup lock. The in-memory `TestClient` clears it
        # so a test can keep registering routes/hooks between requests without
        # tripping `SetupError`; real serving paths leave it on.
        self._setup_lock_enabled = True
        # Single AsyncExitStack driving startup teardown. Entered resources
        # (the lifespan CM, each `on_shutdown` callback, the watchdog) are
        # pushed here in startup order, so a failure mid-startup unwinds only
        # what already succeeded and a clean shutdown unwinds everything in
        # reverse. `None` until the first startup run.
        self._lifespan_stack: contextlib.AsyncExitStack | None = None
        # Mounted Veloce sub-apps started during startup, in start order. Shut
        # down newest-first BEFORE the parent's own on_shutdown handlers, so a
        # child releasing work against a shared resource tears down while that
        # resource is still open (reverse of parent-then-children startup).
        self._started_subapps: list[Veloce] = []
        # App-scoped background tasks spawned via `app.spawn(...)`. Named tasks
        # live in the dict (cancellable / retrievable by name); anonymous ones
        # in the set. Both hold strong references so the loop cannot GC an
        # in-flight task, and both are cancelled-and-drained on shutdown.
        self._spawned_named: dict[str, asyncio.Task[Any]] = {}
        self._spawned_anon: set[asyncio.Task[Any]] = set()

        # Set up logger: the logger name is the
        # `import_name` (already resolved to the caller's module above
        # when not passed explicitly).
        import logging

        self.logger = logging.getLogger(self.import_name)

        self._middlewares: list[Middleware] = []
        # Priority-ordering bookkeeping for `_middlewares`. Each registered
        # middleware records `(priority, sequence, instance)`; `sequence` is a
        # monotonic registration counter so equal priorities keep registration
        # order (a stable tiebreak). `_middleware_seq` issues those sequence
        # numbers and `_any_priority` stays False until a non-zero priority is
        # passed - while it is False, `_middlewares` is exactly the append-order
        # list and the ordered rebuild is skipped entirely, so an app that never
        # uses priorities pays nothing and its ordering is byte-identical to
        # before. Once any priority is set, `_middlewares` is rebuilt at
        # registration time (never per request) as a stable sort by descending
        # priority, so higher-priority middleware runs earlier in the request
        # phase and correspondingly later in the response phase.
        self._middleware_records: list[tuple[int, int, Middleware]] = []
        self._middleware_seq = 0
        self._any_priority = False
        # Monotonic generation counter for `_middlewares`, bumped on every
        # mutation via `add_middleware`. A route's per-route exclusion chain
        # cache (`RouteInfo._mw_chain_cache`) keys on this so a filtered
        # chain is recomputed only when the registered middleware set
        # actually changes, never per request.
        self._mw_version = 0
        # Feature registry + compiled pipeline. `_features` holds the app-level
        # `FeatureSpec` declarations; `_gen` is a monotonic generation counter
        # bumped by every registration funnel; `_pipeline` caches the compiled
        # artifact and is rebuilt lazily when `cp.gen != self._gen`. Generalises
        # the `_mw_version` pattern from middleware-only to all compiled features.
        self._gen = 0
        self._features: list[FeatureSpec] = []
        self._pipeline: CompiledPipeline | None = None
        # WebSocket handshake host / origin gate: pre-filtered from the
        # registered middleware at compile time so the per-connect path iterates
        # a frozen tuple instead of probing every middleware. Enabled only when
        # middleware exists.
        self._features.append(
            FeatureSpec(
                "ws.handshake",
                PH_WS_HANDSHAKE,
                enabled=lambda: bool(self._middlewares),
                build=lambda: build_ws_handshake_checks(self),
            )
        )
        # Response-phase middleware: the `process_response` bound methods in
        # reversed registration order, fused once at compile so no per-response
        # `reversed(self._middlewares)` alloc runs. Consumed only when a request
        # carries no per-route exclusion chain (`_MW_RESPONSE_CHAIN_KEY` absent);
        # an excluded route falls back to the dynamic filtered walk.
        self._features.append(
            FeatureSpec(
                "http.middleware.response",
                PH_HTTP_POST,
                enabled=lambda: bool(self._middlewares),
                build=lambda: build_response_middleware(self),
            )
        )
        # Request-phase middleware: the `process_request` bound methods in
        # forward registration order, fused once. Consumed only when a request
        # carries no per-route exclusion chain; an excluded route uses its
        # dynamic filtered chain.
        self._features.append(
            FeatureSpec(
                "http.middleware.request",
                PH_HTTP_PRE,
                enabled=lambda: bool(self._middlewares),
                build=lambda: build_request_middleware(self),
            )
        )
        # `@app.middleware("http")` call_next chain: the registered functions
        # fused into a tuple. The slot is `None` when none are registered, so the
        # around branch in `handle_request` is taken only when funcs exist.
        self._features.append(
            FeatureSpec(
                "http.middleware.around",
                PH_HTTP_AROUND,
                enabled=lambda: bool(self._http_middleware_funcs),
                build=lambda: tuple(self._http_middleware_funcs),
            )
        )
        # Instrumentation hooks: the registered hooks fused into a tuple. The
        # slot is `None` for an un-instrumented app, which then never reads the
        # perf clock or runs the post-response metrics call.
        self._features.append(
            FeatureSpec(
                "http.instrumentation",
                PH_HTTP_FINISH,
                enabled=lambda: bool(self._instrumentation),
                build=lambda: tuple(self._instrumentation),
            )
        )
        # Standard ASGI middleware wrappers: the registered `(cls, options)`
        # pairs in registration order. Lives at the default `order` so any
        # higher-order wrapper (the live-otel span, registered with a larger
        # `order`) sorts ahead of it and ends up the outermost wrapper - the
        # same position the historical `_asgi_middleware.insert(0, ...)` gave it.
        # The build returns a list of pairs; `_build_asgi_stack` flattens the
        # fused slot into one ordered chain.
        self._features.append(
            FeatureSpec(
                "asgi.middleware",
                PH_ASGI_WRAP,
                enabled=lambda: bool(self._asgi_middleware),
                build=lambda: list(self._asgi_middleware),
            )
        )
        # Standard ASGI middleware - `(class, options)` pairs. Each wraps the
        # whole ASGI application (instantiated as `cls(app, **options)`) and
        # is assembled lazily into `_asgi_stack` on the first request.
        self._asgi_middleware: list[tuple[Any, dict[str, Any]]] = []
        # The assembled ASGI wrapper stack and the generation it was built at.
        # Rebuilt when the compiled pipeline's generation advances (a new ASGI
        # wrapper registered, e.g. the live-otel span), so no wrapper registry
        # needs its own manual stack reset.
        self._asgi_stack: Callable | None = None
        self._asgi_stack_gen: int = -1
        # Observability instrumentation hooks - each is invoked once per
        # finished HTTP request with a `RequestMetrics` record. Empty by
        # default, so an un-instrumented app pays nothing.
        self._instrumentation: list[Callable] = []
        # Per-hook route-template exclusions, populated only when a hook is
        # registered with `exclude_routes`. Sparse on purpose: the common case
        # (no exclusions) leaves this empty so the dispatch loop skips the
        # membership test entirely and a hook with no exclusion pays nothing.
        self._instrumentation_excludes: dict[Callable, frozenset[str]] = {}
        # MCP-only tool registrations (contrib.mcp). Each entry is
        # `(handler, name, description, namespace)`, recorded by
        # `@app.mcp_tool(...)` and consumed once at `mount_mcp` time when the
        # tool registry is assembled.
        self._mcp_tools: list[tuple[Callable, str | None, str | None, str | None]] = []
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
        # Route mutation can flip the mount/static fast-path flags carried on the
        # compiled pipeline, so bump the generation counter here too - the single
        # route-mutation funnel doubles as a pipeline-invalidation sink.
        self._gen += 1

    def _ensure_pipeline(self) -> CompiledPipeline:
        """Return the compiled pipeline, recompiling if the registry changed.

        The generation check is the whole invalidation mechanism: any
        registration bumps `_gen`, so a stale `cp.gen` triggers a rebuild. In
        production `_gen` freezes once the setup lock latches, so this compiles
        exactly once and thereafter is a single int compare.
        """
        cp = self._pipeline
        if cp is None or cp.gen != self._gen:
            cp = self._pipeline = compile_pipeline(self)
        return cp

    def _register_feature_state(self, target: list[Any], value: Any) -> None:
        """Append compiled-feature state and bump the generation counter.

        The single sink for non-route, non-middleware-ledger feature state. It
        ONLY appends and bumps `_gen`; it deliberately does NOT call
        `_assert_mutable` so a caller's existing mutability contract is preserved
        exactly (callers that already assert keep their own assert; callers that
        do not stay unguarded).
        """
        target.append(value)
        self._gen += 1

    def _assert_mutable(self) -> None:
        """Reject setup mutation once the app has started serving.

        A no-op until the first dispatch latches `_setup_locked` (skipped under
        DEBUG/TESTING), so registration during construction pays a single
        boolean check. After the lock trips, route/hook/blueprint registration
        raises `SetupError` rather than racing in-flight requests.
        """
        if self._setup_locked:
            raise SetupError(
                "Cannot register on the application after it has started "
                "serving requests. Move route, hook, blueprint, and middleware "
                "registration to before the first request, or enable DEBUG / "
                "TESTING to allow late changes during development."
            )

    def add_route(self, *args: Any, **kwargs: Any) -> None:
        self._assert_mutable()
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
        self._assert_mutable()
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

        Pass `name=` to override the instance's exclusion name (the identifier
        `exclude_middleware=[...]` on a route references). The override is
        applied *after* construction rather than forwarded into the subclass
        constructor, so per-instance naming works for every `Middleware`
        subclass - including user subclasses whose `__init__` does not accept a
        `name` keyword.

        Pass `priority=` (an `int`, default `0`) to order this middleware
        deterministically regardless of registration order. Higher priority
        runs earlier in the request phase and correspondingly later in the
        response phase; middleware of equal priority keeps registration order
        (a stable tiebreak). The ordered chain is resolved once at registration
        time, so per-request dispatch pays no sorting cost. When no middleware
        sets a priority the behaviour is unchanged - the chain is the plain
        registration order it has always been. `priority` applies to the
        request/response `Middleware` pipeline only, not to ASGI-class
        middleware (which is ordered by its own wrap nesting).
        """
        self._assert_mutable()
        # `priority` is a framework ordering concern, not a construction
        # argument: pop it before any middleware is built so it is never
        # forwarded into a `Middleware` subclass constructor.
        priority = options.pop("priority", 0)
        if isinstance(middleware, type):
            if issubclass(middleware, Middleware):
                # The name override is a framework concern, not a construction
                # argument: pop it so arbitrary subclass constructors (which may
                # not accept `name`) build cleanly, then stamp it on the built
                # instance via the base `name` attribute the exclusion lookup
                # reads.
                name = options.pop("name", None)
                instance = middleware(**options)
                if name is not None:
                    instance.name = name
                self._register_middleware(instance, priority)
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
                # wraps, so defer construction until the stack is built. Bumping
                # the generation counter invalidates the compiled wrap slot and,
                # through the gen-keyed stack cache, the assembled stack too - no
                # separate `_asgi_stack` reset needed.
                self._asgi_middleware.append((middleware, options))
                self._gen += 1
        elif isinstance(middleware, Middleware):
            self._register_middleware(middleware, priority)
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

    def _register_middleware(self, instance: Middleware, priority: int) -> None:
        """Record a built `Middleware` instance and refresh the ordered chain.

        Appends `(priority, sequence, instance)` to the registration ledger and
        bumps the middleware generation counter (so per-route exclusion chains
        recompute). While every registered priority is `0` the ordered chain is
        just the append-order list, so `_middlewares` is updated by a plain
        append and no sort runs - keeping the common no-priority case identical
        to the previous behaviour. The first non-zero priority flips the app
        into ordered mode and rebuilds `_middlewares` as a stable descending
        sort by priority. All of this happens at registration time, never per
        request.
        """
        seq = self._middleware_seq
        self._middleware_seq = seq + 1
        self._middleware_records.append((priority, seq, instance))
        self._mw_version += 1
        # The middleware set drives the WS-handshake phase, so the middleware
        # ledger funnel doubles as a pipeline-invalidation sink.
        self._gen += 1
        if priority and not self._any_priority:
            self._any_priority = True
        if self._any_priority:
            # Stable sort by descending priority: Python's sort is stable, so
            # within an equal priority the registration `seq` order is kept.
            ordered = sorted(self._middleware_records, key=lambda r: -r[0])
            self._middlewares = [rec[2] for rec in ordered]
        else:
            self._middlewares.append(instance)

    def add_instrumentation(
        self,
        hook: Callable | None = None,
        *,
        exclude_routes: Iterable[str] | None = None,
    ) -> Callable:
        """Register an observability instrumentation hook.

        `hook` is called once per finished HTTP request with a
        `RequestMetrics` record - the request method, the concrete path,
        the matched route *template* (a low-cardinality metric label), the
        status code, and the wall-clock duration in milliseconds. It may be
        a plain function or a coroutine function. A hook that raises is
        logged and skipped, so instrumentation never breaks a response.

        Returns `hook` unchanged, so it also works as a decorator. Both the
        no-argument and the keyword-argument decorator forms are supported -
        when `hook` is omitted a decorator is returned that captures
        `exclude_routes` and registers the function it wraps:

            @app.add_instrumentation
            def export(metrics):
                statsd.timing(metrics.route or "unmatched", metrics.duration_ms)

            @app.add_instrumentation(exclude_routes={"/health"})
            def export(metrics):
                statsd.timing(metrics.route or "unmatched", metrics.duration_ms)

        Pass `exclude_routes` to suppress this hook for noisy routes - a set
        of matched route *templates* (e.g. `{"/health", "/metrics"}`). When a
        finished request's route template is in the set the hook is skipped,
        so health checks and scrape endpoints never pollute traces or metric
        series. Matching is on the low-cardinality template resolved during
        routing (never the concrete, attacker-controlled path), so there is
        no per-request regex and no path-normalisation bypass. The filter is
        applied in the core delivery loop, so every consumer of this hook -
        tracing, metrics, access logs, custom - honours the same exclusion.
        An unmatched request (route template `None`) is never excluded by a
        named-route set.

        With no hook registered the request path carries no instrumentation
        cost - not even a clock read.
        """
        # Registration mutates the per-request `_instrumentation` list the
        # dispatch core iterates, so it follows the same setup-lock contract as
        # routes and other hooks: late registration races concurrent dispatch
        # and is rejected (relaxed under DEBUG/TESTING).
        self._assert_mutable()

        # Decorator-with-arguments form: `@app.add_instrumentation(...)` calls
        # this with `hook=None`, so return a decorator that captures the keyword
        # options and registers the function it wraps.
        if hook is None:

            def decorator(fn: Callable) -> Callable:
                return self.add_instrumentation(fn, exclude_routes=exclude_routes)

            return decorator

        self._register_feature_state(self._instrumentation, hook)
        if exclude_routes is not None:
            excluded = frozenset(exclude_routes)
            if excluded:
                self._instrumentation_excludes[hook] = excluded
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

        Read-only compatibility accessor. Useful when conditional setup
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
        """Alias for `_dispatch_request`."""
        return await self._dispatch_request(request, self._ensure_pipeline())

    async def full_dispatch_request(self, request: Request) -> Any:
        """Alias for `_dispatch_request` (which already runs the
        full before/after-request hook chain inline)."""
        return await self._dispatch_request(request, self._ensure_pipeline())

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
        # Lazy import: `veloce.app.contexts` transitively pulls `veloce.http`, which
        # is not yet importable at app module-load time (app -> _contexts -> http).
        from veloce.app.contexts import _AppContext

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
        from veloce.app.contexts import (
            _TestRequestContext,  # lazy: breaks app->_contexts->http cycle
        )

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
        self._register_feature_state(self._http_middleware_funcs, middleware)
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
                self._register_feature_state(self._http_middleware_funcs, func)
                return func

            return decorator
        else:
            if isinstance(middleware_class_or_type, str):
                raise TypeError(
                    "middleware(middleware_class) is the class form; "
                    "@app.middleware('http') is the decorator form"
                )
            self.add_middleware(middleware_class_or_type, **kwargs)

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
        """Snapshot mapping of `bp.name -> Blueprint`.

        Returns a fresh copy, so caller mutations don't affect the
        framework. Re-registering the same name overwrites the previous
        entry.
        """
        return dict(self._blueprints_map)

    def iter_blueprints(self) -> Any:
        """Iterate over every registered `Blueprint`.

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
        self._assert_mutable()
        self._before_request_hooks.append(func)
        self._gen += 1
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
        self._assert_mutable()
        self._before_first_request_hooks.append(func)
        return func

    def after_request(self, func: Callable) -> Callable:
        """Register a function to run after each request."""
        self._assert_mutable()
        self._after_request_hooks.append(func)
        self._gen += 1
        return func

    def teardown_request(self, func: Callable) -> Callable:
        """Register a function to run after request teardown.
        Called with an optional exception argument, even if an exception occurred."""
        self._assert_mutable()
        self._teardown_request_hooks.append(func)
        self._gen += 1
        return func

    def teardown_appcontext(self, func: Callable) -> Callable:
        """Register a function to run on app-context teardown."""
        self._assert_mutable()
        self._teardown_appcontext_hooks.append(func)
        self._gen += 1
        return func

    async def _run_request_teardown(self, exc: BaseException | None, bp_name: str | None) -> None:
        """Run `teardown_request` + `teardown_appcontext` for one request.

        Selects the matched blueprint's `teardown_request` bucket (app-level
        hooks first, then the blueprint's) and then fires the app-level
        `teardown_appcontext` hooks. Hooks always run - even on an exception -
        and receive `exc` (the failing exception or `None`). Shared by the HTTP
        dispatch `finally` and the MCP tool-call path so a route exposed as an
        MCP tool gets the same cleanup an HTTP request gets.
        """
        if self._teardown_request_hooks or self._bp_teardown_hooks:
            if (
                self._bp_teardown_hooks
                and bp_name is not None
                and bp_name in self._bp_teardown_hooks
            ):
                td_hooks: list[Callable] = list(self._teardown_request_hooks)
                td_hooks.extend(self._bp_teardown_hooks[bp_name])
            else:
                td_hooks = list(self._teardown_request_hooks)
        else:
            td_hooks = ()  # type: ignore[assignment]
        if td_hooks:
            await self._run_teardown_hooks(td_hooks, exc, "teardown_request")

        # `teardown_appcontext` fires when the app context pops; in veloce that
        # happens at the end of each request (no separate app/request context
        # split). Hooks receive the exception or None. Errors are logged, never
        # re-raised.
        if self._teardown_appcontext_hooks:
            await self._run_teardown_hooks(
                self._teardown_appcontext_hooks, exc, "teardown_appcontext"
            )

    async def _run_teardown_hooks(
        self, hooks: list[Callable], exc: BaseException | None, label: str
    ) -> None:
        """Run a list of teardown hooks, logging but never raising errors."""
        for hook in hooks:
            try:
                if _is_async_callable(hook):
                    await hook(exc)
                else:
                    await offload(hook, exc)
            except Exception:
                self.logger.exception(f"{label} hook raised an exception")

    # -- URL processors (URL hooks) -----------------------------

    def url_value_preprocessor(self, func: Callable) -> Callable:
        """Register a function `fn(endpoint, values)` that can mutate the
        matched path params before the handler runs.

        Usage::

            @app.url_value_preprocessor
            def pull_lang(endpoint, values):
                from veloce import g
                g.lang = values.pop("lang", "en")

        `endpoint` is the route name; `values` is the path_params dict
        (mutating it in place is the supported way to remove / rewrite
        values before the handler sees them).
        """
        self._assert_mutable()
        self._url_value_preprocessors.append(func)
        self._gen += 1
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

        Usage::

            @app.url_defaults
            def add_lang(endpoint, values):
                from veloce import g
                values.setdefault("lang", g.get("lang", "en"))

        Runs in registration order; mutate `values` in place.
        """
        self._assert_mutable()
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
        self._assert_mutable()
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
            _readd_route(self, full_path, methods, info, endpoint)

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
        # Keep the compiled `is_bare` flag fresh when a blueprint contributes
        # hooks but no routes (a route-bearing blueprint already bumps `_gen`
        # through route registration).
        if (
            blueprint._before_request_hooks
            or blueprint._after_request_hooks
            or blueprint._teardown_request_hooks
        ):
            self._gen += 1

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
        rule: Annotated[
            str,
            Doc("URL path template, including `{param}` / `{param:converter}` placeholders."),
        ],
        endpoint: Annotated[
            str | None,
            Doc("Endpoint name for `url_for`; required when registering an endpoint-only stub."),
        ] = None,
        view_func: Annotated[
            Callable | None,
            Doc(
                "Handler for the route; `None` registers an endpoint-only stub for later attachment."
            ),
        ] = None,
        methods: Annotated[
            list[str] | None,
            Doc("HTTP methods this rule serves; defaults to `GET`."),
        ] = None,
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

        Lifecycle: a mounted *Veloce* sub-app has its startup and shutdown
        driven by the parent - the parent runs each child's startup after its
        own during `lifespan`/`run()` startup, and tears children down in
        reverse on shutdown, so a child's `on_startup` / lifespan resources
        initialise and release without a separate ASGI lifespan. A mounted
        non-Veloce *ASGI* app receives `http` and `websocket` scopes only:
        the parent does not fan the `lifespan` cycle out to it, so it must
        not depend on ASGI `lifespan` events for its setup. A mounted ASGI
        app owns its entire prefix subtree - a native route registered under
        the same prefix is unreachable.

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
            self._register_feature_state(self._mounted_apps, entry)
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
            self._register_feature_state(self._static_handlers, app)
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
        self._register_feature_state(self._asgi_mounts, entry)

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
        Scheduled for removal in v1.0.0.
        """
        if event not in (LIFECYCLE_STARTUP, LIFECYCLE_SHUTDOWN):
            raise ValueError(
                f"event must be {LIFECYCLE_STARTUP!r} or {LIFECYCLE_SHUTDOWN!r}, got {event!r}"
            )
        warnings.warn(
            "Veloce.on_event() is deprecated and will be removed in v1.0.0; "
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
        directly instead. Scheduled for removal in v1.0.0.
        """
        warnings.warn(
            "Veloce.add_event_handler() is deprecated and will be removed "
            "in v1.0.0; use app.on_startup(fn) / app.on_shutdown(fn) instead.",
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
        self._register_feature_state(
            self._static_handlers,
            StaticFiles(directory=directory, prefix=prefix, html=html, must_exist=must_exist),
        )

    # -- MCP (Model Context Protocol) -----------------------------

    def mcp_tool(
        self,
        description: str,
        *,
        name: str | None = None,
        namespace: str | None = None,
    ) -> Callable:
        """Register an MCP-only tool callable by an AI agent (contrib.mcp).

        The decorated coroutine (or sync function) becomes an MCP tool whose
        input JSON Schema is derived from its signature; `Depends()` params
        resolve through the same dependency machinery routes use, with an
        `MCPContext` standing in for the HTTP `Request`. `description` is the
        required LLM-facing text (separate from the docstring). `namespace`
        prefixes the tool name (`<namespace>_<name>`), mirroring how a
        blueprint namespaces an exposed route.

        Usage::

            @app.mcp_tool(description="Add two integers")
            async def add(a: int, b: int) -> int:
                return a + b
        """
        from veloce.contrib.mcp.safety import require_mcp_description

        def decorator(func: Callable) -> Callable:
            require_mcp_description(name or func.__name__, description)
            self._mcp_tools.append((func, name, description, namespace))
            return func

        return decorator

    def mount_mcp(self, transport: str = "stdio") -> Any:
        """Build the MCP server and serve the registered tools.

        Assembles the tool registry from `@app.mcp_tool` registrations plus
        every route flagged `expose_as_mcp_tool=True`, then serves it over the
        chosen transport. Supports `transport="stdio"` only (JSON-RPC 2.0
        on stdin/stdout, for subprocess use); the coroutine runs until stdin
        closes. Returns the awaitable serve coroutine so a caller may schedule
        it explicitly (`asyncio.run(app.mount_mcp())`).

        The serve loop runs inside the app's `lifespan_context()`, so the same
        startup sequence an ASGI server enters - the lifespan context manager
        plus every `on_startup` handler (DB pools, `app.state`, caches) - runs
        before the first tool is served, and the matching shutdown sequence
        runs after stdin closes.
        """
        from veloce.contrib.mcp.server import MCPServer
        from veloce.contrib.mcp.transports.stdio import serve_stdio

        if transport != "stdio":
            raise ValueError(
                f"Unsupported MCP transport {transport!r}; only 'stdio' is supported "
                "(the Streamable HTTP transport is not yet implemented)."
            )
        server = MCPServer(self)

        async def _serve() -> None:
            async with self.lifespan_context():
                await serve_stdio(server)

        return _serve()

    # -- Request handling -----------------------------------------

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

    async def _run_handler(self, handler: Callable[..., Any]) -> None:
        """Invoke a lifecycle handler, offloading sync ones to a thread.

        Async handlers are awaited directly; a plain `def` handler runs in
        the default executor under a copied context so it cannot block the
        event loop. Shared by the startup and shutdown paths so the two stay
        in lockstep.
        """
        if _is_async_callable(handler):
            await handler()
        else:
            await offload(handler)

    async def _run_lifecycle(self, event: str) -> None:
        """Run lifecycle event handlers, including the lifespan context manager.

        Startup acquires the user lifespan CM and the dev watchdog onto a single
        `AsyncExitStack` stored on the app. A startup handler that raises mid-way
        unwinds exactly what was already acquired (the stack closes in reverse)
        before the error propagates, so a partially-started app leaves no
        orphaned resources. Shutdown drains any `app.spawn(...)` tasks, runs
        every `on_shutdown` handler (one raising never skips the rest), then
        closes the stack to exit the CM and stop the watchdog - collecting every
        failure and re-raising them grouped, so no teardown error is masked.
        """
        if event == LIFECYCLE_STARTUP:
            stack = contextlib.AsyncExitStack()
            try:
                # The lifespan CM is entered first so it exits last, after every
                # on_shutdown handler has run - resources it provides outlive the
                # handlers that use them.
                if self._lifespan is not None:
                    self._lifespan_cm = self._lifespan(self)
                    await stack.enter_async_context(self._lifespan_cm)

                for handler in self._on_startup:
                    await self._run_handler(handler)

                # Fan startup out to every mounted Veloce sub-app. A mounted
                # child is dispatched through the parent pipeline and never
                # receives its own ASGI lifespan, so without this its
                # `on_startup` / lifespan resources would never initialise. Each
                # child's startup runs after the parent's own; the started
                # children are recorded so shutdown can tear them down
                # newest-first BEFORE the parent's on_shutdown handlers (and so a
                # mid-fan-out failure unwinds the already-started ones). A
                # non-Veloce ASGI mount owns its own lifecycle and is skipped.
                # The same child instance mounted under multiple prefixes is
                # started and shut down only once (deduped by identity).
                self._started_subapps = []
                _seen_subs: set[int] = set()
                for _prefix, _prefix_slash, _sub in self._mounted_apps:
                    if isinstance(_sub, Veloce) and id(_sub) not in _seen_subs:
                        _seen_subs.add(id(_sub))
                        await _sub._run_lifecycle(LIFECYCLE_STARTUP)
                        self._started_subapps.append(_sub)

                # Dev-mode event-loop blocking watchdog - opt-in, so an app
                # that does not set the config key never builds one. The key
                # may be a plain truthy value, or a mapping of watchdog kwargs
                # (`interval`, `stall_threshold`) for tuning. Registered on the
                # stack so it is always stopped, even on partial-startup failure.
                _wd_config = self.config.get("EVENT_LOOP_WATCHDOG")
                if _wd_config and self._watchdog is None:
                    from veloce.watchdog import EventLoopWatchdog

                    _wd_kwargs = dict(_wd_config) if isinstance(_wd_config, Mapping) else {}
                    self._watchdog = EventLoopWatchdog(asyncio.get_running_loop(), **_wd_kwargs)
                    self._watchdog.start()
                    stack.push_async_callback(self._stop_watchdog)
            except BaseException:
                # Unwind whatever startup acquired before the failure, then let
                # the original error propagate so the ASGI/native caller emits
                # the startup-failed signal. Unwind errors must not mask the
                # startup failure itself. Already-started children come down
                # first (newest-first), then the parent's acquired-resource stack.
                with contextlib.suppress(Exception):
                    await self._shutdown_subapps()
                with contextlib.suppress(Exception):
                    await stack.aclose()
                self._lifespan_cm = None
                raise
            self._lifespan_stack = stack
        else:
            shutdown_stack = self._lifespan_stack
            self._lifespan_stack = None
            errors: list[BaseException] = []
            try:
                # Cancel and drain parent-owned spawned / supervised background
                # tasks FIRST, before mounted children tear down, so a parent
                # background loop cannot keep touching child-owned state after the
                # child has closed. The `finally` drain below still catches any
                # task a teardown handler spawns (the registries are cleared, so
                # this early drain and the late one do not double-cancel).
                await self._drain_spawned_tasks()
                # Tear mounted sub-apps down next (newest-first), before the
                # parent's own on_shutdown handlers run - reverse of the
                # parent-then-children startup order, so a shared resource a
                # parent shutdown handler closes is still available while each
                # child releases work against it.
                errors.extend(await self._shutdown_subapps())
                # Run every on_shutdown handler, newest first (symmetric to the
                # startup order), collecting failures so one raising teardown
                # does not abort the rest - unlike a bare loop that stops on
                # first error.
                for handler in reversed(self._on_shutdown):
                    try:
                        await self._run_handler(handler)
                    except BaseException as exc:  # noqa: BLE001 - aggregated below
                        errors.append(exc)
                # Close the acquired-resource stack (lifespan CM exit + watchdog
                # stop). When no startup ran (standalone or repeat shutdown) the
                # stack is absent; fall back to stopping the watchdog and exiting
                # an open CM directly so standalone shutdown still tears
                # everything down.
                self._lifespan_cm = None
                if shutdown_stack is not None:
                    try:
                        await shutdown_stack.aclose()
                    except BaseException as exc:  # noqa: BLE001 - aggregated below
                        errors.extend(_collect_chained(exc))
                else:
                    await self._stop_watchdog()
            finally:
                # Drain spawned tasks LAST, after the on_shutdown handlers and
                # lifespan teardown have completed, so any task a teardown
                # callback spawned via `app.spawn(...)` is also drained instead
                # of surviving past shutdown. In a `finally` so the drain still
                # runs (with the same timeout/cancel behavior) even when a
                # teardown raised above.
                await self._drain_spawned_tasks()
            _raise_unwind_errors(errors)

    async def _shutdown_subapps(self) -> list[BaseException]:
        """Shut down started mounted sub-apps newest-first; return any errors.

        Every child is torn down even if one raises (errors aggregated and
        returned to the caller), and the started list is cleared so a repeat or
        standalone shutdown does not re-run them.
        """
        errors: list[BaseException] = []
        for sub in reversed(self._started_subapps):
            try:
                await sub._run_lifecycle(LIFECYCLE_SHUTDOWN)
            except BaseException as exc:  # noqa: BLE001 - aggregated by the caller
                errors.extend(_collect_chained(exc))
        self._started_subapps = []
        return errors

    async def _stop_watchdog(self) -> None:
        """Stop and clear the dev watchdog. Registered on the lifespan stack."""
        if self._watchdog is not None:
            self._watchdog.stop()
            self._watchdog = None

    def lifespan_context(self) -> _LifespanManager:
        """Return an async context manager driving the lifespan cycle.

        `async with app.lifespan_context(): ...` runs the full startup
        sequence (lifespan CM enter + `on_startup` handlers) on entry
        and the shutdown sequence on exit - independent of any request.
        Useful for tests and for embedding the app where you want
        startup/shutdown without an ASGI server in the loop.
        """
        from veloce.app.contexts import _LifespanManager  # lazy: breaks app->_contexts->http cycle

        return _LifespanManager(self)

    # -- ASGI compatibility layer ---------------------------------
