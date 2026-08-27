"""Veloce application — the main entry point."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import os
import sys
import warnings
import weakref
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from veloce._internal import (
    _UNRESOLVED_JSON_DUMPS,
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
    URL_SCHEME_HTTP,
)
from veloce.app.asgi import AsgiMixin
from veloce.app.background import BackgroundTasksMixin
from veloce.app.dispatch import DispatchMixin
from veloce.app.errors import ErrorsMixin
from veloce.app.lifecycle import LifecycleMixin
from veloce.app.mcp import MCPMixin
from veloce.app.middleware import MiddlewareMixin
from veloce.app.mounting import MountingMixin
from veloce.app.openapi import OpenAPIMixin
from veloce.app.plugins import PluginsMixin
from veloce.app.serving import ServingMixin
from veloce.app.templating import TemplatingMixin
from veloce.app.testing import TestingMixin
from veloce.app.urls import URLRule as URLRule
from veloce.app.urls import _URLMap
from veloce.audit import run as audit_run
from veloce.blueprints import _endpoint_blueprint, _resolve_scoped_chain
from veloce.exceptions import (
    BuildError,
    SetupError,
)
from veloce.helpers import Aborter, g, jsonify, send_from_directory, send_from_directory_async
from veloce.http.datastructures import State
from veloce.http.request import Request
from veloce.http.response import (
    Response,
)
from veloce.routing.router import Router, _readd_route

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.icons import Icon
    from veloce.contrib.staticfiles import StaticFiles
    from veloce.middleware import Middleware


@functools.lru_cache(maxsize=1)
def _constructor_parameter_names() -> frozenset[str]:
    """Every real `Veloce()` parameter name, read once."""
    import inspect

    return frozenset(
        name
        for name, parameter in inspect.signature(Veloce.__init__).parameters.items()
        if parameter.kind is not parameter.VAR_KEYWORD and name != "self"
    )


def _warn_on_misspelled_parameters(extra: dict[str, Any]) -> None:
    """Warn when an `**extra` key looks like a misspelling of a real parameter.

    `**extra` is an open namespace, so an unknown key is not an error - an
    extension may legitimately put anything there. A key one edit away from a
    real parameter name is a different thing: `Veloce(tittle="My API")` was
    accepted, stashed where nothing reads it, and the app served the default
    title with no error, no warning and no log line.

    Only reached when `extra` is non-empty, so an app that passes none pays
    nothing - neither the imports nor the signature read, which is why both are
    deferred. The parameter names are read once and cached: introspecting a
    35-parameter `Annotated` signature costs ~140us, and an app using `**extra`
    legitimately would otherwise pay that on every construction.
    """
    import difflib

    known = _constructor_parameter_names()
    for key in extra:
        # A similarity ratio is `2M / (len(a) + len(b))` with `M <= min(len)`, so
        # 0.8 is unreachable once one name is more than half again as long as the
        # other. Filtering on that first keeps the comparison off the names that
        # cannot match - most of them, for a typical extension key.
        span = range(int(len(key) * 0.66), int(len(key) * 1.5) + 1)
        candidates = [name for name in known if len(name) in span]
        close = difflib.get_close_matches(key, candidates, n=1, cutoff=0.8) if candidates else []
        if close:
            warnings.warn(
                f"Veloce() got {key!r}, which is not a parameter - did you mean "
                f"{close[0]!r}? It has been kept in `app.extra`, so nothing reads it "
                f"and {close[0]!r} keeps its default.",
                UserWarning,
                stacklevel=3,
            )


class Veloce(
    AsgiMixin,
    DispatchMixin,
    ErrorsMixin,
    LifecycleMixin,
    MCPMixin,
    MiddlewareMixin,
    MountingMixin,
    OpenAPIMixin,
    PluginsMixin,
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
        website_url: Annotated[
            str | None,
            Doc(
                "Page describing this server, published in the MCP `serverInfo` so a "
                "client can link to it."
            ),
        ] = None,
        mcp_icons: Annotated[
            Sequence[Icon] | None,
            Doc(
                "Icons published in the MCP `serverInfo`, for a client rendering this "
                "server beside others."
            ),
        ] = None,
        debug: Annotated[
            bool,
            Doc("Enable debug mode: verbose error pages and development conveniences."),
        ] = False,
        prefix: Annotated[
            str,
            Doc(
                "Path prefix prepended to every route registered on the app, including"
                " the documentation and MCP routes. It does not apply to `app.mount()`,"
                " which places a sub-application at the prefix it is given."
            ),
        ] = "",
        docs_url: Annotated[
            str | None,
            Doc(
                "Path serving the Swagger UI docs page; `None` or an empty string"
                " disables it, as does disabling `openapi_url`, since the page has no"
                " schema to read. Cannot equal `redoc_url`."
            ),
        ] = "/docs",
        redoc_url: Annotated[
            str | None,
            Doc(
                "Path serving the ReDoc docs page; `None` or an empty string disables"
                " it, as does disabling `openapi_url`. Cannot equal `docs_url`."
            ),
        ] = "/redoc",
        openapi_url: Annotated[
            str | None,
            Doc(
                "Path serving the generated OpenAPI JSON document; `None` or an empty"
                " string disables it, and disables `docs_url` and `redoc_url` with it -"
                " both pages read this document."
            ),
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
        # `app.extra` for extensions / OpenAPI customisation to read. The
        # namespace is open by design, so a key that is merely unknown is fine -
        # but one that is a near-miss of a real parameter is almost certainly a
        # typo, and absorbing `tittle=` silently meant the app ran with the
        # default title and nothing said so.
        if extra:
            _warn_on_misspelled_parameters(extra)
        self.extra: dict[str, Any] = dict(extra)
        # instance folder - explicit override, else computed from
        # `package_root` on first `instance_path` access.
        # Refused rather than resolved: the computed default is rooted, and a
        # relative override would resolve against the working directory the
        # process happened to be launched from - so the same deployment would put
        # its SQLite file and uploads somewhere different depending on where it
        # was started. The location of a per-deployment writable directory is not
        # something to leave to that.
        #
        # A leading separator counts as rooted even where `os.path.isabs` says
        # otherwise: on Windows `isabs("/srv/app")` is False because there is no
        # drive, but the path is still not relative to the working directory - and
        # refusing it would reject a POSIX deployment path written on a Windows
        # development machine, which is the ordinary case here.
        if instance_path is not None and not (
            os.path.isabs(instance_path) or instance_path[:1] in ("/", "\\")
        ):
            raise ValueError(
                f"instance_path must be an absolute path, got {instance_path!r}; "
                f"it names a per-deployment writable directory, and a relative path "
                f"would resolve against the current working directory."
            )
        self._instance_path = instance_path
        # `import_name` - defaults to the caller's module so
        # `Veloce(__name__)` works. Used to compute `root_path` (the
        # package directory) for template / static-file resolution.
        if import_name is None:
            frame = sys._getframe(1)
            import_name = frame.f_globals.get("__name__", "veloce.app")
        self.import_name = import_name
        # Both are REQUIRED strings in the document these build (OpenAPI 3.1
        # Sec. 4.8.2) and both are interpolated into the two HTML pages, where a
        # non-string reached `html.escape` and answered 500. Refused here, at the
        # one place they enter, rather than on a request to `/docs`.
        for _field, _value in (("title", title), ("version", version)):
            if not isinstance(_value, str) or not _value:
                raise ValueError(
                    f"{_field} must be a non-empty string, got {_value!r}; it is a "
                    f"required field of the OpenAPI document and appears on /docs."
                )
        self.title = title
        self.version = version
        # Identity the MCP `serverInfo` publishes beyond name and version. Held on
        # the app so one server describes itself in one place, whichever door.
        self.website_url = website_url
        self.mcp_icons = mcp_icons
        self.description = description
        # OpenAPI 3.1 Sec. 4.8.2 `info.summary` - a short one-line summary
        # of the API, distinct from the longer `description`.
        self.summary = summary
        # An empty string disables a UI, the same way `openapi_url` is already
        # guarded on truthiness. Registered instead, `docs_url=""` mounted Swagger
        # at the site root - and if the app also owned `/`, the collision only
        # surfaced on the first request, because the doc routes register lazily.
        self._docs_url = docs_url or None
        self._redoc_url = redoc_url or None
        if self._docs_url is not None and self._docs_url == self._redoc_url:
            raise ValueError(
                f"docs_url and redoc_url are both {self._docs_url!r}; two documentation "
                f"pages cannot share a path. Give them different paths, or pass None to "
                f"disable one."
            )
        self._openapi_url = openapi_url
        self._openapi_setup = False
        self.openapi_schema: dict[str, Any] | None = None
        self.redirect_slashes = redirect_slashes
        # Normalised once here rather than at each reader: a trailing slash
        # would double the separator in every URL built from it, and the value
        # is compared and concatenated in several places.
        self.root_path = "/" + root_path.strip("/") if root_path.strip("/") else ""
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
        self.extensions: dict[str, Any] = {}
        self._lifespan = lifespan
        # Additional lifespan context managers contributed by plugins and
        # extensions. Entered on the same exit stack as `lifespan=`, so they
        # inherit its reverse-order teardown and error aggregation.
        self._extra_lifespans: list[Any] = []
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
            # `app/` is core and `contrib/` is optional, so this is deferred to keep the
            # layering: importing the optional integration eagerly would make every
            # `import veloce` pay for machinery most apps never mount.
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
        self._init_lifecycle_state()
        self._init_middleware_state()
        self._init_feature_pipeline()
        self._init_asgi_stack_state()
        self._init_mcp_state()
        self._init_introspection_caches()

    def _init_lifecycle_state(self) -> None:
        """Lifespan handles, the setup lock, and the spawned-task ledgers."""
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

    def _init_middleware_state(self) -> None:
        """The app logger and the middleware ledger its registration funnels write."""
        # Set up logger: the logger name is the
        # `import_name` (already resolved to the caller's module above
        # when not passed explicitly).
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

    def _init_feature_pipeline(self) -> None:
        """The feature registry and the pipeline compiled from it.

        The `FeatureSpec` declarations are appended in order and read back as a
        sequence, so they stay together here rather than being split by topic.
        """
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

    def _init_asgi_stack_state(self) -> None:
        """The ASGI wrapper stack and the observability hooks around it."""
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

    def _init_introspection_caches(self) -> None:
        """The watchdog handle, error-handler maps, and the lazy route caches."""
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
        # Sub-apps mounted with `expose_mcp=True`, whose MCP primitives are
        # published through this app's own MCP server under a name prefix.
        self._mcp_mounts: list[tuple[str, Any]] = []
        # Tools handed over already built rather than derived from a signature:
        # an upstream's, discovered by `add_mcp_proxy`, or one narrowed by
        # `derive_tool` and registered with `add_mcp_tool`. Their schema is
        # whatever built them, so the registry adds them as they are.
        self._mcp_prebuilt_tools: list[Any] = []
        self._http_middleware_funcs: list[Callable] = []  # @app.middleware("http") funcs
        # Jinja2 helper registrations - applied to the env on each render.
        self._template_filters: list[tuple[str, Callable]] = []
        self._template_globals: list[tuple[str, Callable]] = []
        self._template_tests: list[tuple[str, Callable]] = []
        # URL processors: preprocessor runs after route match and
        # can mutate path_params (e.g. pop a lang segment into g); url_defaults
        # runs inside url_for/url_path_for and can inject default kwargs.
        # Objects that report to `veloce check` without being middleware or a
        # static handler. A mounted MCP endpoint registers routes, so the audit
        # had nothing to ask about a tool-execution endpoint with no auth.
        self._auditables: list[Any] = []
        self._url_value_preprocessors: list[Callable] = []
        self._url_default_funcs: list[Callable] = []
        # Blueprint-contributed URL processors, bucketed by the endpoint's
        # dotted blueprint name exactly as the request hooks are. Merged into
        # the app lists as gated closures, every blueprint's processor was
        # tested on every request - the O(blueprints * processors) of no-op
        # work the hook buckets exist to avoid - and one of them cost every
        # route in the app its straight-line dispatch.
        self._bp_url_value_preprocessors: dict[str, list[Callable]] = {}
        self._bp_url_default_funcs: dict[str, list[Callable]] = {}
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
        # Resolved on first use from the provider above: `None` means "the
        # default provider with nothing configured", which is byte-for-byte what
        # the direct orjson path already emits - so an app that configured
        # nothing keeps that path and pays only a `None` test. Anything else is
        # the provider's own `dumps`, so a configured dialect reaches a handler's
        # `dict` / `list` / model return the way it already reaches `jsonify`.
        self._handler_json_dumps: Any = _UNRESOLVED_JSON_DUMPS
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
        """Register a route. See `Router.add_route` for the full signature.

        This override exists only to bracket the base implementation: refuse a
        mutation once the app is serving, and drop the route caches the new
        route invalidates. It forwards everything untouched, so the parameters,
        defaults and `Doc(...)` annotations are the base method's.

        `__doc__` and `__signature__` are re-pointed at the base just below, so
        `help(app.add_route)` and an editor's signature hint show the documented
        surface rather than `(*args, **kwargs)`. Restating the parameter list
        here instead would add a ninth hand-maintained copy of it.
        """
        self._assert_mutable()
        super().add_route(*args, **kwargs)
        self._invalidate_route_caches()

    # Introspection recovers the documented signature the `*args` forward hides.
    add_route.__signature__ = inspect.signature(Router.add_route)  # type: ignore[attr-defined]

    def include_router(self, router: Any, prefix: str = "", url_prefix: str | None = None) -> None:
        """Mount a sub-router under an optional path prefix.

        Accepts either a `Blueprint` (delegates to `register_blueprint`,
        honouring its hooks / error handlers / url processors) or a plain
        `Router` (delegates to `Router.include_router`).

        `prefix` and `url_prefix` are interchangeable and name the same thing;
        both spellings are accepted so router-style and blueprint-style calling
        code can each use the one that reads naturally, and `url_prefix` wins
        when both are given.
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

    def security_audit(self) -> list[str]:
        """Return human-readable warnings about the current security posture.

        A rendering of `veloce.audit.run(self)`, which is the structured form -
        severity, remedy and a stable id per finding. Use that where a tool
        needs to tell an `error` from a `warning`; this stays a list of lines
        to print. An empty list means nothing was flagged.

        Middleware reports on itself through `Middleware.audit`, so a
        middleware written outside this package is audited on the same terms
        as a built-in one. What stays invisible is hardening the app does not
        own - a reverse proxy terminating TLS, or a middleware that reports
        nothing - so a clean audit is a statement about this app's middleware,
        not about the deployment around it.
        """
        return [str(finding) for finding in audit_run(self)]

    def response_contract_audit(self) -> list[str]:
        """Return human-readable findings about each route's response contract.

        A rendering of the `response-model-contradiction` and
        `routes-undocumented` findings from `veloce.audit.run(self)`, which is
        the structured form. A route whose `response_model=` names a different
        model than its return annotation is a `warning`; a route with no
        response contract at all is `info`, because many such routes are
        legitimate - HTML pages, redirects, streams.

        An empty list means nothing was flagged.
        """
        contract_ids = {"response-model-contradiction", "routes-undocumented"}
        return [str(finding) for finding in audit_run(self) if finding.id in contract_ids]

    @property
    def json(self) -> Any:
        """Active `JSONProvider` instance.

        Lazily instantiated from `app.json_provider_class` so swapping
        encoders is just: `app.json_provider_class = MyJSONProvider`.
        Setting `app.json = instance` replaces it explicitly.

        The provider serialises every value a handler returns - a `dict`, a
        `list`, a model, a msgspec struct, a `(body, status)` tuple, `jsonify`,
        and a `JSONResponse` subclass named by `response_class` - so one dialect
        covers the application. An app that configures nothing keeps the direct
        orjson path and pays nothing for the indirection.

        It does not reach the framework's own wire formats: cache keys always
        sort so equal mappings hash alike, and signed cookies, JWTs and protocol
        frames are not the application's to restyle.
        """
        if self._json_provider is None:
            self._json_provider = self.json_provider_class(self)
        return self._json_provider

    @json.setter
    def json(self, provider: Any) -> None:
        self._json_provider = provider

    def _resolve_handler_json_dumps(self) -> Any:
        """The serialiser a handler's JSON return must use, or `None` for direct.

        `None` is returned when the active provider is the stock one with no
        configured options, because the direct path already produces exactly
        what it would. Resolved once: like the provider itself, the options are
        read when first asked for, so set them before the first request.
        """
        # Deferred like the sibling import above it: `json_provider` reaches
        # back into the app package.
        from veloce.json_provider import resolve_dumps

        return resolve_dumps(self)

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
        directory = self.static_folder
        if not os.path.isabs(directory):
            directory = os.path.join(self.package_root, directory)
        return await send_from_directory_async(directory, filename)

    @property
    def package_root(self) -> str:
        """Filesystem path of the directory containing `import_name`'s module.

        Named `package_root` rather than `root_path` because `Veloce.root_path`
        already means the ASGI mount prefix, which is a different thing entirely
        - one is a location on disk, the other a URL prefix. Useful for resolving
        template and static directories relative to the app's source file.
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

        `app.aborter(404)` is equivalent to the module-level `abort(404)` helper.
        It is a distinct attribute so applications can subclass `Aborter` to add
        custom code-to-exception mappings: assign to `app.aborter`, or mutate the
        instance this returns.

        One instance per application, built on first access and kept - so a
        mutation to its mapping sticks, and does not reach any other app.
        """
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
                    "app.cli requires `click`. Install it with: pip install veloceframework[cli]"
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
                "test_cli_runner() requires `click`. Install it with: pip install veloceframework[cli]"
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
        full before/after-request hook chain inline).
        """
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
        # Read directly: `endpoint` is a `Request.__slots__` field assigned in
        # `__init__`, so the `getattr` default could never apply - and reading it
        # the same way `_run_before_hooks` does keeps the two walks comparable.
        bp = _endpoint_blueprint(request.endpoint)
        if bp is not None and self._bp_before_hooks:
            for hook in self._bp_before_hooks.get(bp, ()):
                result = await self._call_handler(hook, {"request": request})
                if result is not None:
                    return result
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Run all `after_request` hooks for `(request, response)`.

        Hooks fire in **reverse** registration order; each hook may return a
        replacement `Response` (the contract: any other return keeps the
        existing one). App-level hooks reverse-iterate first, then the matched
        blueprint's, then the request's one-shot `after_this_request` callbacks.

        This is the dispatch path itself, not a re-implementation of it, so a
        hook behaves here exactly as it will in production - including the
        signature adaptation that lets a hook declare only the arguments it
        wants.
        """
        return await self._run_after_hooks(request, response, _endpoint_blueprint(request.endpoint))

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
        - anything else -> JSON, matching what dispatch does with the same
          value returned from a handler

        `veloce.make_response` applies the same table; dispatch keeps its own
        fast lanes for the shapes a handler returns most, but answers alike.
        """
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
        # Anything else is JSON-encoded, which is what a handler returning the
        # same value already gets from dispatch. Raising here made the public
        # coercer refuse `123` and `None` while a handler returning them was
        # answered `200` with a JSON body - one framework, two answers for one
        # value.
        return jsonify(value)

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

        Returns `{blueprint_name_or_None: [fn, ...]}` - app-level processors
        under `None`, then each blueprint's under its dotted name. A nested
        blueprint's entry is the flattened chain that applies to its routes,
        outermost first, which is what runs.
        """
        view: dict[Any, list[Callable]] = {None: list(self._url_value_preprocessors)}
        view.update({name: list(fns) for name, fns in self._bp_url_value_preprocessors.items()})
        return view

    @property
    def url_default_functions(self) -> dict[Any, list[Callable]]:
        """View of registered URL-default callbacks, keyed as `url_value_preprocessors`."""
        view: dict[Any, list[Callable]] = {None: list(self._url_default_funcs)}
        view.update({name: list(fns) for name, fns in self._bp_url_default_funcs.items()})
        return view

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

    def _absolute_url_defaults(self) -> tuple[str | None, str]:
        """Answer `Router`'s absolute-URL hook from this app's configuration."""
        return (
            self.config.get("SERVER_NAME"),
            self.config.get("PREFERRED_URL_SCHEME", URL_SCHEME_HTTP),
        )

    def url_for(self, name: str, /, **path_params: Any) -> str:
        """`Veloce.url_for` runs `@app.url_defaults` callbacks before
        delegating to `Router.url_for`, so injected defaults appear in the
        rendered URL.

        On build failure (unknown endpoint or missing path parameter),
        each registered `app.url_build_error_handlers` callback is
        invoked with `(error, endpoint, values)` in order; the first
        non-None return is used. If none recovers, a `BuildError` is
        raised.
        """
        bp_defaults = None
        if self._bp_url_default_funcs:
            bp = _endpoint_blueprint(name)
            if bp is not None:
                bp_defaults = self._bp_url_default_funcs.get(bp)
        if self._url_default_funcs or bp_defaults:
            # Copy so the callbacks can mutate without changing the caller's
            # kwargs dict.
            values = dict(path_params)
            for fn in self._url_default_funcs:
                fn(name, values)
            for fn in bp_defaults or ():
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
    def url_path_for(self, name: str, /, **path_params: Any) -> str:
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
        - Buckets the blueprint's `before_request` / `after_request` /
          `teardown_request` hooks under its name, so they fire only for that
          blueprint's own routes. They are kept apart from the app-level lists
          rather than spliced into them and filtered: a per-request scan of every
          hook is what bucketing avoids.
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
        already_registered = bp_name in self._blueprints_map
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
        # walking every blueprint's gated wrapper on every request. Keyed by
        # blueprint name so the dispatcher reads `_bp_before_hooks[bp_name]`
        # directly: a flat list of gated closures would test every hook of
        # every blueprint on every request, which is O(blueprints * hooks) of
        # no-op work for an app with many of either.
        # A nested child's hooks are bucketed under the child's own dotted path,
        # not the parent's, so they reach the child's routes and not a sibling's.
        # The ancestor chain is flattened here rather than walked per request:
        # `<bp>.<child>` holds the blueprint's hooks followed by the child's, so
        # dispatch still does one lookup with one key.
        # Mounting the same blueprint twice is supported and re-registers its
        # routes under the second prefix - but every bucket below is keyed by the
        # blueprint's *name*, and both mounts give their routes the same
        # `<bpname>.` endpoint prefix. So one lookup finds one bucket, and
        # appending to it a second time ran every hook twice on a single
        # request: a rate-limit or audit `before_request` double-counted.
        # The routes above are per-mount; everything from here down is per-name.
        for own_attr, scoped_attr, bucket in (
            ()
            if already_registered
            else (
                ("_before_request_hooks", "_scoped_before_hooks", self._bp_before_hooks),
                ("_after_request_hooks", "_scoped_after_hooks", self._bp_after_hooks),
                ("_teardown_request_hooks", "_scoped_teardown_hooks", self._bp_teardown_hooks),
                (
                    "_url_value_preprocessors",
                    "_scoped_url_value_preprocessors",
                    self._bp_url_value_preprocessors,
                ),
                ("_url_default_funcs", "_scoped_url_default_funcs", self._bp_url_default_funcs),
            )
        ):
            own = getattr(blueprint, own_attr)
            scoped = getattr(blueprint, scoped_attr)
            if own:
                bucket.setdefault(bp_name, []).extend(own)
            for suffix in scoped:
                chain = _resolve_scoped_chain(own, scoped, suffix)
                if chain:
                    bucket.setdefault(f"{bp_name}.{suffix}", []).extend(chain)
            if own or scoped:
                # Keep the compiled `is_bare` flag fresh when a blueprint
                # contributes hooks but no routes (a route-bearing blueprint
                # already bumps `_gen` through route registration).
                self._gen += 1

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
            # A class-based view already knows its verbs: `View.as_view` sets
            # `view.methods` from the methods the class actually defines. Reading
            # it here is what makes that assignment mean something - without it a
            # `MethodView` defining `get` and `post` was registered for GET alone
            # and answered its own POST with 405.
            methods=methods or getattr(view_func, "methods", None) or [HTTP_METHOD_GET],
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

    # ── ASGI compatibility layer ──────────────────────────
