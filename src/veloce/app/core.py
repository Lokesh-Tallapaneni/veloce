"""Veloce application — the main entry point."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import sys
import weakref
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from veloce._internal import (
    MIME_HTML,
    _coerce_bool,
    _is_async_callable,
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
    ROUTE_METHOD_WEBSOCKET,
)
from veloce.app.asgi import AsgiMixin
from veloce.app.background import BackgroundTasksMixin
from veloce.app.dispatch import DispatchMixin
from veloce.app.errors import ErrorsMixin
from veloce.app.lifecycle import LifecycleMixin
from veloce.app.middleware import MiddlewareMixin
from veloce.app.mounting import MountingMixin
from veloce.app.openapi import OpenAPIMixin
from veloce.app.serving import ServingMixin
from veloce.app.templating import TemplatingMixin
from veloce.app.testing import TestingMixin
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
from veloce.middleware import Middleware
from veloce.routing.router import Router, _readd_route

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.icons import Icon


# Sentinel for cache misses where `None` is itself a valid cache hit
# (e.g. "no exception handler matched this type"). Plain `cache.get(k)`
# would re-walk the MRO every time for an unhandled exception type.
_MISSING: Any = object()


class Veloce(
    AsgiMixin,
    DispatchMixin,
    ErrorsMixin,
    LifecycleMixin,
    MiddlewareMixin,
    MountingMixin,
    OpenAPIMixin,
    ServingMixin,
    TestingMixin,
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
        self.extensions: dict[str, Any] = {}  # Extensions registry
        self._lifespan = lifespan
        self._init_runtime_state()
        # `exception_handlers=` ctor mapping - keys are
        # exception classes or integer status codes.
        for _key, _handler in (exception_handlers or {}).items():
            self.add_exception_handler(_key, _handler)
        # ASGI shape `middleware=` ctor list - each entry is
        # a middleware instance applied in the given order.
        for _mw in middleware or []:
            self.add_middleware(_mw)
        self._init_registries()
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

    def _init_runtime_state(self) -> None:
        """Initialise the app's internal runtime and pipeline state.

        The lifecycle bookkeeping, middleware ledger, compiled feature
        pipeline specs, instrumentation / MCP / error-handler tables, and the
        route-introspection caches. Arg-free: every value is an empty
        container, a sentinel, or a FeatureSpec whose enabled/build lambdas
        read app state lazily at request time. Called once from __init__.
        """
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
        # `(handler, name, description, namespace, scopes, icons, task_support)`,
        # recorded by `@app.mcp_tool(...)` and consumed once at `mount_mcp` time
        # when the tool registry is assembled.
        self._mcp_tools: list[
            tuple[Callable, str | None, str | None, str | None, frozenset[str] | None, Any, bool]
        ] = []
        # MCP prompt registrations (contrib.mcp). Each entry is
        # `(handler, name, description, namespace, scopes, icons)`, recorded by
        # `@app.mcp_prompt(...)` and consumed once at `mount_mcp` time when the
        # prompt registry is assembled.
        self._mcp_prompts: list[
            tuple[Callable, str | None, str | None, str | None, frozenset[str] | None, Any]
        ] = []
        # MCP argument-completer registrations (contrib.mcp). Each entry is
        # `(kind, key, argument, completer)` where `kind` is "prompt" or
        # "resource", `key` is the prompt name or resource URI, recorded by
        # `@app.mcp_completer(...)` and bound onto its descriptor at `mount_mcp`
        # time so `completion/complete` can answer for that argument.
        self._mcp_completers: list[tuple[str, str, str, Callable]] = []
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

    def _init_registries(self) -> None:
        """Initialise the per-app hook, mount, template, and URL registries.

        The request and blueprint hooks, mount lists, template and URL
        processor registries, and the lazily-built helpers (webhooks router,
        JSON provider, aborter, static config). Arg-free; called once from
        __init__ after the constructor-time handler/middleware registration.
        """
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
        # Per-blueprint error handlers, kept out of the app-global tables so a
        # blueprint's handler only catches exceptions raised by its own (or a
        # nested descendant's) routes - consulted by `request.blueprints` before
        # the app-level tables, never across sibling blueprints.
        self._bp_exception_handlers: dict[str, dict[type, Callable]] = {}
        self._bp_status_handlers: dict[str, dict[int, Callable]] = {}
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

    # ── Properties ────────────────────────────────────────

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
    def secret_key(self) -> str | None:
        """Session-signing secret; bound to `config['SECRET_KEY']`.

        `SessionMiddleware` constructed without an explicit `secret_key=`
        resolves it from here on the first request, so `app.secret_key = ...`
        and `config['SECRET_KEY']` are one and the same setting.
        """
        return self.config.get("SECRET_KEY")

    @secret_key.setter
    def secret_key(self, value: str | None) -> None:
        self.config["SECRET_KEY"] = value

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

    # ── Route caches and the compiled pipeline ─────────────

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

    # ── Registration APIs ──────────────────────────────────

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

    # ── Security posture ───────────────────────────────────

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
            warnings.append(
                "SECRET_KEY is not set - session middleware that does not pass "
                "its own secret_key= cannot sign cookies (set app.secret_key)."
            )
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

    # ── JSON, static files, and Jinja ──────────────────────

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

    # ── Signals, aborter, and CLI ──────────────────────────

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
                    "app.cli requires `click` - install with: pip install veloceframework[cli]"
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
                "test_cli_runner() requires `click`. Install with: pip install veloceframework[cli]"
            ) from err
        return CliRunner(**kwargs)

    # ── Dispatch aliases and response coercion ─────────────

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

    # ── Endpoint and hook introspection ────────────────────

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
                    # Rebuild the plans through the same path registration
                    # uses, so every dispatch flag - including
                    # `is_fast_eligible`, which depends on the handler being a
                    # coroutine function - reflects the replacement handler
                    # rather than the stub it displaced.
                    self._finalize_plans(info, is_ws=_method.upper() == ROUTE_METHOD_WEBSOCKET)
            if not replaced:
                raise ValueError(f"No route registered for endpoint {name!r}")
            # The name -> handler map served by `view_functions` may have been
            # built against the stub; drop it so the next read sees `func`.
            self._cached_view_functions = None
            return func

        return decorator

    @property
    def error_handler_spec(self) -> dict[Any, dict[Any, Callable]]:
        """Inspection view of registered error handlers.

        Returns a `{blueprint_name_or_None: {key: handler}}` mapping.
        App-level handlers live under the `None` key; each blueprint's
        handlers live under the blueprint's name, keyed by integer status
        code or exception class. Blueprint handlers are scoped to their own
        routes at dispatch time, so they appear under their blueprint name
        here, not folded into `None`.
        """
        merged: dict[Any, Callable] = {}
        merged.update(self._status_handlers)
        merged.update(self._exception_handlers)
        result: dict[Any, dict[Any, Callable]] = {None: merged}
        for bp_name in set(self._bp_status_handlers) | set(self._bp_exception_handlers):
            sub: dict[Any, Callable] = {}
            sub.update(self._bp_status_handlers.get(bp_name, {}))
            sub.update(self._bp_exception_handlers.get(bp_name, {}))
            result[bp_name] = sub
        return result

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

    # ── URL processors (URL hooks) ────────────────────────

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

    # ── Blueprints and URL rules ───────────────────────────

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
        - Buckets blueprint-level error handlers under the blueprint name (and
          each nested child under its dotted name), scoped to that blueprint's
          own routes; an app-level handler still catches everything as a fallback.

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

        # Error handlers stay scoped to the blueprint's own routes: bucket them
        # under the blueprint name rather than merging into the app-global
        # tables, so the dispatch error path (consulting `request.blueprints`)
        # finds them only for a request on this blueprint or a descendant - never
        # on a sibling blueprint or an app-level route. A nested child's handlers
        # are bucketed under the child's full dotted name (`<bp>.<child>`), so two
        # sibling children do not share a single parent bucket.
        if blueprint._exception_handlers:
            self._bp_exception_handlers.setdefault(bp_name, {}).update(
                blueprint._exception_handlers
            )
        if blueprint._status_handlers:
            self._bp_status_handlers.setdefault(bp_name, {}).update(blueprint._status_handlers)
        for suffix, table in blueprint._scoped_exception_handlers.items():
            self._bp_exception_handlers.setdefault(f"{bp_name}.{suffix}", {}).update(table)
        for suffix, status_table in blueprint._scoped_status_handlers.items():
            self._bp_status_handlers.setdefault(f"{bp_name}.{suffix}", {}).update(status_table)

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

    # ── Dependency overrides (for testing) ────────────────

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

    # ── MCP (Model Context Protocol) ──────────────────────

    def mcp_tool(
        self,
        description: str,
        *,
        name: str | None = None,
        namespace: str | None = None,
        scopes: Sequence[str] | None = None,
        icons: Sequence[Icon] | None = None,
        task_support: bool = False,
    ) -> Callable:
        """Register an MCP-only tool callable by an AI agent (contrib.mcp).

        The decorated coroutine (or sync function) becomes an MCP tool whose
        input JSON Schema is derived from its signature; `Depends()` params
        resolve through the same dependency machinery routes use, with an
        `MCPContext` standing in for the HTTP `Request`. `description` is the
        required LLM-facing text (separate from the docstring). `namespace`
        prefixes the tool name (`<namespace>_<name>`), mirroring how a
        blueprint namespaces an exposed route. `icons` is an optional list of
        `Icon` objects a client may render next to the tool. `task_support=True`
        lets a client run the tool as a background task (task-augmented
        `tools/call`, polled via `tasks/get` / `tasks/result`).

        Usage::

            @app.mcp_tool(description="Add two integers")
            async def add(a: int, b: int) -> int:
                return a + b
        """
        from veloce.contrib.mcp.safety import require_mcp_description

        scope_set = frozenset(scopes) if scopes else None

        def decorator(func: Callable) -> Callable:
            require_mcp_description(name or func.__name__, description)
            self._mcp_tools.append(
                (func, name, description, namespace, scope_set, icons, task_support)
            )
            return func

        return decorator

    def mcp_prompt(
        self,
        description: str,
        *,
        name: str | None = None,
        namespace: str | None = None,
        scopes: Sequence[str] | None = None,
        icons: Sequence[Icon] | None = None,
    ) -> Callable:
        """Register an MCP prompt template fetchable by an AI agent (contrib.mcp).

        The decorated callable's parameters become the prompt's arguments, and its
        return - a string, or a list of role/content messages - becomes the
        messages ``prompts/get`` returns. `Depends()` params resolve through the
        same dependency machinery routes use, with an `MCPContext` standing in for
        the HTTP `Request`. `description` is the required LLM-facing text;
        `namespace` prefixes the prompt name (`<namespace>_<name>`). `icons` is an
        optional list of `Icon` objects a client may render next to the prompt.

        Usage::

            @app.mcp_prompt(description="Summarise a topic in three bullets")
            async def summarise(topic: str) -> str:
                return f"Summarise {topic} in three bullet points."
        """
        from veloce.contrib.mcp.safety import require_mcp_description

        scope_set = frozenset(scopes) if scopes else None

        def decorator(func: Callable) -> Callable:
            require_mcp_description(name or func.__name__, description)
            self._mcp_prompts.append((func, name, description, namespace, scope_set, icons))
            return func

        return decorator

    def mcp_completer(
        self,
        *,
        argument: str,
        prompt: str | None = None,
        resource: str | None = None,
    ) -> Callable:
        """Register an argument-value completer for an MCP prompt or resource (contrib.mcp).

        The decorated callable suggests values for one `argument` of a `prompt`
        (named) or a `resource` (by URI template) as the user types, answering the
        MCP ``completion/complete`` request. It is called with the partial value
        and a mapping of the sibling argument values already resolved, and returns
        a sequence of candidate strings (or a `CompletionResult` for explicit
        totals). Pass exactly one of `prompt` or `resource`. An argument with no
        registered completer answers with an empty completion.

        Usage::

            @app.mcp_completer(prompt="greet", argument="name")
            async def complete_name(value: str, context: dict[str, str]) -> list[str]:
                return [n for n in KNOWN_NAMES if n.startswith(value)]
        """
        if prompt is not None and resource is None:
            kind, key = "prompt", prompt
        elif resource is not None and prompt is None:
            kind, key = "resource", resource
        else:
            raise ValueError("mcp_completer requires exactly one of prompt= or resource=.")

        def decorator(func: Callable) -> Callable:
            self._mcp_completers.append((kind, key, argument, func))
            return func

        return decorator

    def mount_mcp(
        self,
        transport: str = "stdio",
        *,
        path: str = "/mcp",
        auth: Any = None,
        principal: Any = None,
        allowed_origins: Sequence[str] | None = None,
        exclude_middleware: Sequence[str] | None = None,
        sessions: bool = False,
        resumable: bool = False,
    ) -> Any:
        """Build the MCP server and serve the registered tools.

        Assembles the tool registry from `@app.mcp_tool` registrations plus every
        route flagged `expose_as_mcp_tool=True`, the resource registry from every
        read-only route flagged `expose_as_mcp_resource=True`, and the prompt
        registry from `@app.mcp_prompt` registrations, then serves them over the
        chosen transport.

        `transport="stdio"` (the default) serves JSON-RPC 2.0 on stdin/stdout for
        subprocess use and returns an awaitable serve coroutine that runs until
        stdin closes, inside the app's `lifespan_context()` - so every
        `on_startup` handler runs before the first tool is served. Schedule it
        explicitly (`asyncio.run(app.mount_mcp())`). A local subprocess is trusted,
        so authentication is from the environment: pass a `principal` (a
        `veloce.Principal`) to establish the identity / scopes the served tools run
        under.

        `transport="http"` mounts the Streamable HTTP transport as a `POST` route
        at `path` (default `/mcp`) on this app and returns `None`; serve the app
        with any ASGI server (or `app.run()`) as usual. Pass `auth` (a
        `veloce.contrib.mcp.MCPAuth`) to make the endpoint an OAuth 2.1 resource
        server - validating the bearer token on every request and serving the
        RFC 9728 metadata. `allowed_origins` enables `Origin` validation
        (DNS-rebinding defense); `exclude_middleware` names app middleware the
        transport routes opt out of (an app-wide auth middleware `auth` replaces).
        `sessions` opts into `Mcp-Session-Id` lifecycle: the server assigns a
        session id on `initialize`, requires it on later requests (400 missing,
        404 once terminated), and accepts a `DELETE` to terminate it.
        `resumable` opts into SSE resumability: each streamed event gets an id
        encoding its stream, and a `GET` carrying `Last-Event-ID` replays only that
        stream's missed events so a client can reconnect after a dropped connection.
        Call this after the tool / resource / prompt routes are registered.
        """
        from veloce.contrib.mcp.server import MCPServer

        if transport == "stdio":
            from veloce.contrib.mcp.transports.stdio import serve_stdio
            from veloce.principal import set_principal

            server = MCPServer(self)

            async def _serve() -> None:
                if principal is not None:
                    set_principal(principal)
                async with self.lifespan_context():
                    await serve_stdio(server)

            return _serve()

        if transport == "http":
            from veloce.contrib.mcp.transports.http import register_http_transport

            server = MCPServer(self)
            # A task-augmented call records the creating connection's identity and
            # the follow-up tasks/get|result|list|cancel must run under that same
            # connection. The stateless default mints a throwaway session (a fresh,
            # never-recycled connection id) per POST, so a task created by one POST
            # can never be retrieved by another. Require sessions=True so the
            # connection persists and the task remains reachable.
            if not sessions and any(tool.task_support for tool in server.registry.tools.values()):
                raise ValueError(
                    "MCP task support over the HTTP transport requires sessions=True; "
                    "pass mount_mcp(transport='http', sessions=True) so a task created "
                    "by one request can be retrieved by the follow-up tasks/* request."
                )

            register_http_transport(
                self,
                server,
                path=path,
                auth=auth,
                allowed_origins=(
                    frozenset(allowed_origins) if allowed_origins is not None else None
                ),
                exclude_middleware=exclude_middleware,
                sessions=sessions,
                resumable=resumable,
            )
            return None

        raise ValueError(
            f"Unsupported MCP transport {transport!r}; supported transports are 'stdio' and 'http'."
        )

    # ── ASGI compatibility layer ──────────────────────────
