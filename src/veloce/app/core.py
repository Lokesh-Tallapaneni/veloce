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
    _coerce_bool,
    _readd_route,
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
)
from veloce.app.asgi import AsgiMixin
from veloce.app.background import BackgroundTasksMixin
from veloce.app.dispatch import DispatchMixin
from veloce.app.errors import ErrorsMixin
from veloce.app.introspection import IntrospectionMixin
from veloce.app.lifecycle import LifecycleMixin
from veloce.app.mcp import MCPMixin
from veloce.app.middleware import MiddlewareMixin
from veloce.app.mounting import MountingMixin
from veloce.app.openapi import OpenAPIMixin
from veloce.app.plugins import PluginsMixin
from veloce.app.serving import ServingMixin
from veloce.app.templating import TemplatingMixin
from veloce.app.testing import TestingMixin
from veloce.app.urls import URLMap
from veloce.audit import run as audit_run
from veloce.blueprints import Blueprint, _resolve_scoped_chain
from veloce.exceptions import (
    SetupError,
)
from veloce.helpers import Aborter, send_from_directory, send_from_directory_async
from veloce.http.datastructures import State
from veloce.http.request import Request
from veloce.routing.router import Router

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.icons import Icon
    from veloce.contrib.staticfiles import StaticFiles
    from veloce.middleware import Middleware


@functools.lru_cache(maxsize=1)
def _constructor_parameter_names() -> frozenset[str]:
    """Every real `Veloce()` parameter name, read once."""
    return frozenset(
        name
        for name, parameter in inspect.signature(Veloce.__init__).parameters.items()
        if parameter.kind is not parameter.VAR_KEYWORD and name != "self"
    )


def _warn_on_misspelled_parameters(extra: dict[str, Any]) -> None:
    """Warn when an `**extra` key looks like a misspelling of a real parameter.

    `**extra` is an open namespace, so an unknown key is not an error; a key one
    edit away from a real parameter name almost certainly is. Only reached when
    `extra` is non-empty, and the names are cached - introspecting the
    35-parameter `Annotated` signature costs ~140us.
    """
    import difflib

    known = _constructor_parameter_names()
    for key in extra:
        # A similarity ratio is `2M / (len(a) + len(b))` with `M <= min(len)`, so
        # 0.8 is unreachable once one name is half again as long as the other.
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
    IntrospectionMixin,
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

    # The version string emitted in the document, and nothing else: the schema
    # objects are written in JSON Schema 2020-12, the dialect OpenAPI 3.1 adopts,
    # so a 3.0 version here declares a dialect the document does not speak.
    openapi_version: str = "3.1.0"

    # Built lazily; declared so the type is not set by whichever method runs first.
    _cached_routes: list[dict[str, Any]] | None
    _cached_view_functions: dict[str, Callable[..., Any]] | None
    _cached_url_map: URLMap | None
    _json_provider: Any
    _aborter: Any
    _cli_group: Any

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
            Callable[..., Any] | None,
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
            dict[Any, Callable[..., Any]] | None,
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
        super().__init__(
            prefix=prefix,
            default_response_class=default_response_class,
            dependencies=dependencies,
            responses=responses,
            on_duplicate=on_duplicate,
        )
        if extra:
            _warn_on_misspelled_parameters(extra)
        self.extra: dict[str, Any] = dict(extra)
        # Refused rather than resolved: a relative override would resolve against
        # the working directory the process was launched from. A leading separator
        # counts as rooted even where `os.path.isabs` says otherwise - on Windows
        # `isabs("/srv/app")` is False for want of a drive, yet it is not relative.
        if instance_path is not None and not (
            os.path.isabs(instance_path) or instance_path[:1] in ("/", "\\")
        ):
            raise ValueError(
                f"instance_path must be an absolute path, got {instance_path!r}; "
                f"it names a per-deployment writable directory, and a relative path "
                f"would resolve against the current working directory."
            )
        self._instance_path = instance_path
        # Feeds `package_root` (template / static-file resolution) and the logger.
        if import_name is None:
            frame = sys._getframe(1)
            # A frame's globals are untyped, so a non-string `__name__` falls back too.
            caller = frame.f_globals.get("__name__")
            import_name = caller if isinstance(caller, str) else "veloce.app"
        self.import_name = import_name
        # Both are REQUIRED strings in the document these build (OpenAPI 3.1
        # Sec. 4.8.2), and both are interpolated into the two HTML pages.
        for _field, _value in (("title", title), ("version", version)):
            if not isinstance(_value, str) or not _value:
                raise ValueError(
                    f"{_field} must be a non-empty string, got {_value!r}; it is a "
                    f"required field of the OpenAPI document and appears on /docs."
                )
        self.title = title
        self.version = version
        self.website_url = website_url
        self.mcp_icons = mcp_icons
        self.description = description
        # OpenAPI 3.1 Sec. 4.8.2 `info.summary`, distinct from `description`.
        self.summary = summary
        # An empty string disables a UI; registered, `docs_url=""` mounts at `/`.
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
        # Normalised once: a trailing slash would double the separator in every URL.
        self.root_path = "/" + root_path.strip("/") if root_path.strip("/") else ""
        self.openapi_tags = openapi_tags
        self.openapi_external_docs = openapi_external_docs
        self.servers = servers
        self.license_info = license_info
        self.contact = contact
        self.terms_of_service = terms_of_service
        self.swagger_ui_parameters = swagger_ui_parameters
        self.swagger_ui_init_oauth = swagger_ui_init_oauth
        self.separate_input_output_schemas = separate_input_output_schemas
        self.disambiguate_operation_ids = disambiguate_operation_ids
        self.validate_openapi = validate_openapi

        from veloce.config import Config

        self.state: State = State()
        # Seeded with the documented defaults so `app.config[k]` never raises.
        self.config: Config = Config(Config.default_config())
        # `app.debug` is a property over this key, so the two cannot drift apart.
        self.config["DEBUG"] = debug
        self.extensions: dict[str, Any] = {}
        self._lifespan = lifespan
        # Entered on the same exit stack as `lifespan=`, so teardown is reversed.
        self._extra_lifespans: list[Any] = []
        self._init_runtime_state()
        for _key, _handler in (exception_handlers or {}).items():
            self.add_exception_handler(_key, _handler)
        for _mw in middleware or []:
            self.add_middleware(_mw)
        self._init_registries()
        self.template_folder: str | None = template_folder
        self._templates = None
        if template_folder is not None:
            # Deferred to keep the layering: `app/` is core, `contrib/` is not.
            from veloce.contrib.templating import Jinja2Templates

            tdir = template_folder
            if not os.path.isabs(tdir):
                tdir = os.path.join(self.package_root, tdir)
            self._templates = Jinja2Templates(directory=tdir)

    # ── Properties ────────────────────────────────────────

    @property
    def debug(self) -> bool:
        """Whether debug mode is enabled; bound to `config['DEBUG']`.

        A dotenv-style string (`DEBUG=false`) is interpreted rather than treated
        as truthy because it is non-empty.
        """
        return _coerce_bool(self.config.get("DEBUG", False))

    @debug.setter
    def debug(self, value: bool) -> None:
        # Coerced like the getter, so `app.debug = "false"` stores False.
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
    def url_map(self) -> URLMap:
        """Read-only mapping of registered URL rules.

        Iterating yields `URLRule` objects (rule, methods, endpoint);
        subscripting by endpoint name (`app.url_map["users.detail"]`) returns
        that endpoint's rules, and `len()` is the total route count. The
        introspection-friendly view of `Veloce.routes`.
        """
        cached = self._cached_url_map
        if cached is None:
            cached = URLMap(self)
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

        Called from `add_route` and `include_router`; every higher-level
        registration funnels through one of the two.
        """
        self._cached_routes = None
        self._cached_view_functions = None
        self._cached_url_map = None
        # Route mutation can flip the compiled pipeline's fast-path flags.
        self._gen += 1

    def _ensure_pipeline(self) -> CompiledPipeline:
        """Return the compiled pipeline, recompiling if the registry changed.

        Any registration bumps `_gen`, so a stale `cp.gen` triggers a rebuild.
        In production `_gen` freezes once the setup lock latches, leaving a
        single int compare per request.
        """
        cp = self._pipeline
        if cp is None or cp.gen != self._gen:
            cp = self._pipeline = compile_pipeline(self)
        return cp

    def _register_feature_state(self, target: list[Any], value: Any) -> None:
        """Append compiled-feature state and bump the generation counter.

        The single sink for non-route, non-middleware-ledger feature state. It
        deliberately does NOT call `_assert_mutable`, so each caller keeps its
        own mutability contract.
        """
        target.append(value)
        self._gen += 1

    def _assert_mutable(self) -> None:
        """Reject setup mutation once the app has started serving.

        A no-op until the first dispatch latches `_setup_locked` (skipped under
        DEBUG/TESTING), so registration during construction costs one boolean
        check; after the lock trips, registration would race in-flight requests.
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

        Brackets the base implementation: refuse a mutation once the app is
        serving, then drop the caches the new route invalidates. Everything is
        forwarded untouched, so the parameters, defaults and `Doc(...)`
        annotations are the base method's.
        """
        self._assert_mutable()
        super().add_route(*args, **kwargs)
        self._invalidate_route_caches()

    # Introspection recovers the documented signature the `*args` forward hides.
    add_route.__signature__ = inspect.signature(Router.add_route)  # type: ignore[attr-defined]

    def include_router(
        self,
        router: Annotated[
            Router,
            Doc("Sub-router to mount; a `Blueprint` brings its hooks and handlers with it."),
        ],
        prefix: Annotated[str, Doc("Path prefix every mounted route is registered under.")] = "",
        url_prefix: Annotated[
            str | None, Doc("Blueprint-style spelling of `prefix`; wins when both are given.")
        ] = None,
    ) -> None:
        """Mount a sub-router under an optional path prefix.

        A `Blueprint` delegates to `register_blueprint`, honouring its hooks,
        error handlers and url processors; a plain `Router` delegates to
        `Router.include_router`. `prefix` and `url_prefix` name the same thing,
        and `url_prefix` wins when both are given.
        """
        self._assert_mutable()

        effective = url_prefix if url_prefix is not None else (prefix or None)
        if isinstance(router, Blueprint):
            self.register_blueprint(router, url_prefix=effective)
        else:
            Router.include_router(self, router, prefix=effective or "")
            self._invalidate_route_caches()

    @property
    def instrumentation_hooks(self) -> tuple[Callable[..., Any], ...]:
        """The registered instrumentation hooks, in the order they will run.

        The read half of `add_instrumentation`, for a caller checking whether it
        has already installed its own hook. A tuple, so a caller cannot register
        or reorder through it; use `add_instrumentation` for that.
        """
        return tuple(self._instrumentation)

    def add_instrumentation(
        self,
        hook: Callable[..., Any] | None = None,
        *,
        exclude_routes: Iterable[str] | None = None,
    ) -> Callable[..., Any]:
        """Register an observability instrumentation hook.

        `hook` is called once per finished HTTP request with a
        `RequestMetrics` record - the request method, the concrete path,
        the matched route *template* (a low-cardinality metric label), the
        status code, and the wall-clock duration in milliseconds. It may be
        a plain function or a coroutine function. A hook that raises is
        logged and skipped, so instrumentation never breaks a response.

        Returns `hook` unchanged, so it also works as a decorator, in either
        the no-argument or the keyword-argument form:

            @app.add_instrumentation
            def export(metrics):
                statsd.timing(metrics.route or "unmatched", metrics.duration_ms)

            @app.add_instrumentation(exclude_routes={"/health"})
            def export(metrics):
                statsd.timing(metrics.route or "unmatched", metrics.duration_ms)

        `exclude_routes` suppresses this hook for noisy routes - a set of
        matched route *templates* (e.g. `{"/health", "/metrics"}`), so health
        checks and scrape endpoints never pollute traces or metric series.
        Matching is on the low-cardinality template resolved during routing,
        never the concrete attacker-controlled path, so there is no per-request
        regex and no path-normalisation bypass; an unmatched request is never
        excluded by a named-route set. The filter is applied in the core
        delivery loop, so every consumer of this hook honours it.

        With no hook registered the request path carries no instrumentation
        cost - not even a clock read.
        """
        self._assert_mutable()

        if hook is None:

            def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
                return self.add_instrumentation(fn, exclude_routes=exclude_routes)

            return decorator

        self._register_feature_state(self._instrumentation, hook)
        if exclude_routes is not None:
            excluded = frozenset(exclude_routes)
            if excluded:
                self._instrumentation_excludes[hook] = excluded
        return hook

    # ── Security posture ───────────────────────────────────

    def register_auditable(self, component: Any) -> Any:
        """Register `component` to report to `veloce check` and `security_audit`.

        Middleware reports on itself through `Middleware.audit` without being
        registered anywhere - it is already in the stack. This is for something
        that hardens or exposes the app *without* being middleware, such as a
        mounted MCP endpoint, which registers routes instead.

        `component` needs an `audit(ctx)` yielding `Finding`s, exactly as a
        middleware's does; it may also carry the `Auditable` class markers
        (`sets_hardening_headers`, `audit_needs_routes`). Returns `component`,
        so it can be used as a decorator on a class.

        Usage::

            class TelemetryEndpoint:
                def audit(self, ctx):
                    if not self.authenticated:
                        yield Finding(
                            "telemetry endpoint accepts unauthenticated writes",
                            severity="error",
                            fix="pass auth=...",
                            id="telemetry-unauthenticated",
                        )

            app.register_auditable(TelemetryEndpoint())
        """
        self._assert_mutable()
        self._auditables.append(component)
        return component

    def security_audit(self) -> list[str]:
        """Return human-readable warnings about the current security posture.

        A rendering of `veloce.audit.run(self)`, which is the structured form -
        severity, remedy and a stable id per finding. Use that where a tool
        needs to tell an `error` from a `warning`; this stays a list of lines
        to print. An empty list means nothing was flagged.

        Middleware reports on itself through `Middleware.audit`, so one written
        outside this package is audited on the same terms as a built-in. What
        stays invisible is hardening the app does not own - a reverse proxy
        terminating TLS - so a clean audit is a statement about this app's
        middleware, not about the deployment around it.
        """
        return [str(finding) for finding in audit_run(self)]

    def response_contract_audit(self) -> list[str]:
        """Return human-readable findings about each route's response contract.

        A rendering of the `response-model-contradiction` and
        `routes-undocumented` findings from `veloce.audit.run(self)`. A route
        whose `response_model=` names a different model than its return
        annotation is a `warning`; a route with no response contract at all is
        `info`, because many such routes are legitimate - HTML pages,
        redirects, streams. An empty list means nothing was flagged.
        """
        contract_ids = {"response-model-contradiction", "routes-undocumented"}
        return [str(finding) for finding in audit_run(self) if finding.id in contract_ids]

    @property
    def json(self) -> Any:
        """Active `JSONProvider` instance.

        Lazily instantiated from `app.json_provider_class` so swapping
        encoders is just: `app.json_provider_class = MyJSONProvider`.
        Setting `app.json = instance` replaces it explicitly.

        The provider serialises every value a handler returns, so one dialect
        covers the application; an app that configures nothing keeps the direct
        orjson path and pays nothing for the indirection. It does not reach the
        framework's own wire formats: cache keys always sort so equal mappings
        hash alike, and signed cookies, JWTs and protocol frames are not the
        application's to restyle.
        """
        if self._json_provider is None:
            self._json_provider = self.json_provider_class(self)
        return self._json_provider

    @json.setter
    def json(self, provider: Any) -> None:
        self._json_provider = provider

    def _resolve_handler_json_dumps(self) -> Any:
        """Resolve the serialiser a handler's JSON return must use, or `None`.

        `None` is returned when the active provider is the stock one with no
        configured options, because the direct path already produces exactly
        what it would. Resolved once, so set the options before the first
        request.
        """
        # Deferred: `json_provider` reaches back into the app package.
        from veloce.json_provider import resolve_dumps

        return resolve_dumps(self)

    def send_static_file(self, filename: str) -> Any:
        """Serve a file from `app.static_folder`.

        `app.static_folder` defaults to `"static"` (relative to
        `app.package_root`). Returns a `FileResponse`; traversal-safe via
        `safe_join`. Reads the file synchronously and emits a
        `DeprecationWarning` when called on a running loop, so async handlers
        should prefer `send_static_file_async`.
        """
        directory = self.static_folder
        if not os.path.isabs(directory):
            directory = os.path.join(self.package_root, directory)
        return send_from_directory(directory, filename)

    async def send_static_file_async(self, filename: str) -> Any:
        """Serve a file from `app.static_folder` - async variant.

        Reads the file in an executor, so it never blocks the event loop.
        """
        directory = self.static_folder
        if not os.path.isabs(directory):
            directory = os.path.join(self.package_root, directory)
        return await send_from_directory_async(directory, filename)

    @property
    def package_root(self) -> str:
        """Filesystem path of the directory containing `import_name`'s module.

        Named `package_root` rather than `root_path` because `Veloce.root_path`
        already means the ASGI mount prefix - one is a location on disk, the
        other a URL prefix.
        """
        mod = sys.modules.get(self.import_name)
        mod_file = getattr(mod, "__file__", None) if mod else None
        if mod_file:
            return os.path.dirname(os.path.abspath(mod_file))
        return os.getcwd()

    @property
    def instance_path(self) -> str:
        """Writable instance folder beside the package.

        `<package_root>/instance`, for config, SQLite files, uploads and the
        like, overridden by an explicit `instance_path=` constructor argument.
        The directory is *not* auto-created - the caller decides whether to
        `mkdir` it.
        """
        if self._instance_path is not None:
            return self._instance_path
        return os.path.join(self.package_root, "instance")

    # ── Signals, aborter, and CLI ──────────────────────────

    @property
    def signal_namespace(self) -> Any:
        """Accessor that returns the `veloce.signals` module.

        The signals are module-level singletons, so
        `app.signal_namespace.request_started` is the same `Signal` instance as
        `veloce.signals.request_started`.
        """
        from veloce import signals

        return signals

    @property
    def aborter(self) -> Any:
        """Callable that raises typed `HTTPException`s by status code.

        `app.aborter(404)` is equivalent to the module-level `abort(404)` helper.
        It is a distinct attribute so applications can subclass `Aborter` to add
        custom code-to-exception mappings: assign to `app.aborter`, or mutate the
        instance this returns. One instance per application, built on first
        access and kept, so a mutation to its mapping sticks and reaches no
        other app.
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

        Read-only, for conditional setup that depends on whether the app has
        bootstrapped yet.
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

        The `veloce` console script discovers and mounts the group as a `custom`
        subcommand when launched with an app reference. `click` is required at
        access time but not at import time, so an environment that does not need
        the CLI never imports it.
        """
        if self._cli_group is None:
            try:
                import click
            except ImportError as err:  # pragma: no cover
                raise RuntimeError(
                    "app.cli requires `click`. Install it with: pip install veloceframework[cli]"
                ) from err
            self._cli_group = click.Group(
                name=self.title.lower().replace(" ", "-"),
                help=f"Custom CLI for {self.title}.",
            )
        return self._cli_group

    def test_cli_runner(self, **kwargs: Any) -> Any:
        """Return a Click `CliRunner` bound for testing `app.cli`.

        Kwargs flow through to `click.testing.CliRunner`.
        """
        try:
            from click.testing import CliRunner
        except ImportError as err:  # pragma: no cover
            raise RuntimeError(
                "test_cli_runner() requires `click`. Install it with: pip install veloceframework[cli]"
            ) from err
        return CliRunner(**kwargs)

    # ── Blueprints and URL rules ───────────────────────────

    def register_blueprint(
        self,
        blueprint: Annotated[Blueprint, Doc("Blueprint whose routes and hooks are mounted.")],
        url_prefix: Annotated[
            str | None, Doc("Path prefix to mount under; defaults to the blueprint's own.")
        ] = None,
    ) -> None:
        """Mount a `Blueprint`'s routes + hooks onto this app.

        Each route is re-registered under `(url_prefix or bp.url_prefix) + path`,
        so the same blueprint can be mounted twice (e.g. v1/v2 versions). Its
        request hooks and error handlers are bucketed under its name - and each
        nested child under its dotted name - so they fire only for that
        blueprint's own routes; an app-level error handler still catches
        everything as a fallback.

        Mountable multiple times on different apps with different
        prefixes - the blueprint itself stays unmodified.
        """
        self._assert_mutable()

        if not isinstance(blueprint, Blueprint):
            raise TypeError(
                f"register_blueprint expects a Blueprint, got {type(blueprint).__name__}"
            )

        effective_prefix = url_prefix if url_prefix is not None else blueprint.url_prefix
        bp_name = blueprint.name
        # `already_registered` is identity, not name: re-mounting *this* blueprint
        # at a second prefix must not append its hooks twice. A different
        # blueprint sharing the name is refused instead, since both would take the
        # same `<name>.` endpoint prefix and their hook buckets would collide.
        existing = self._blueprints_map.get(bp_name)
        if existing is not None and existing is not blueprint:
            raise ValueError(
                f"a different blueprint named {bp_name!r} is already registered: both would "
                f"share the {bp_name!r} endpoint prefix, so their routes and hooks cannot be "
                "told apart. Give one a different name."
            )
        already_registered = existing is not None
        self._blueprints_map[bp_name] = blueprint

        # `<bpname>.` prefixes the name so `url_for` and hook-gating can find it.
        for path, methods, info in blueprint._walk_routes():
            full_path = (effective_prefix or "") + path
            endpoint = f"{bp_name}.{info.name}"
            _readd_route(self, full_path, methods, info, endpoint)

        # Bucketed by name so dispatch looks hooks up by the matched route's
        # endpoint prefix rather than testing every blueprint's gated wrapper per
        # request. A nested child's ancestor chain is flattened under its own
        # dotted path. The routes above are per-mount, these per-name - hence the gate.
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
                # Keeps `is_bare` fresh for a blueprint with hooks but no routes.
                self._gen += 1

        # Bucketed rather than merged into the app-global tables, so the dispatch
        # error path finds them only for this blueprint or a descendant; a nested
        # child's go under its own dotted name, not a shared parent bucket.
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
            Callable[..., Any] | None,
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
            # `View.as_view` sets `view.methods` from the methods the class
            # defines, so a class-based view is registered for all of them.
            methods=methods or getattr(view_func, "methods", None) or [HTTP_METHOD_GET],
            name=endpoint,
            **kwargs,
        )

    # ── Dependency overrides (for testing) ────────────────

    def dependency_overrides_provider(self) -> dict[Callable[..., Any], Callable[..., Any]]:
        """Return the dependency override mapping."""
        return self._dependency_overrides

    @property
    def dependency_overrides(self) -> dict[Callable[..., Any], Callable[..., Any]]:
        """Mutable map of dependency callables to test replacements.

        Populate it to swap a real dependency for a fake one in tests::

            app.dependency_overrides[get_db] = get_fake_db

        The resolver consults this map on every request, so changes take
        effect immediately. Assigning a fresh dict (or calling `.clear()`)
        removes all overrides.
        """
        return self._dependency_overrides

    @dependency_overrides.setter
    def dependency_overrides(self, value: dict[Callable[..., Any], Callable[..., Any]]) -> None:
        self._dependency_overrides = value
        # The previous callables' sub-plans are unreachable but still pin them.
        self._override_subplans.clear()

    # ── Construction helpers ──────────────────────────────

    def _init_runtime_state(self) -> None:
        """Initialise the app's internal runtime and pipeline state.

        Arg-free: every value is an empty container, a sentinel, or a
        `FeatureSpec` whose lambdas read app state lazily. Called once from
        `__init__`.
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
        # Flipped True on the first dispatch, so late registration raises rather
        # than racing in-flight requests. Assigned before the ctor-time
        # `exception_handlers=` / `middleware=` registration reads it.
        self._setup_locked = False
        # Master switch: the in-memory `TestClient` clears it so a test can keep
        # registering between requests; real serving paths leave it on.
        self._setup_lock_enabled = True
        # Entered resources are pushed in startup order, so a failure mid-startup
        # unwinds only what succeeded and a clean shutdown unwinds in reverse.
        self._lifespan_stack: contextlib.AsyncExitStack | None = None
        # In start order, and shut down newest-first BEFORE the parent's own
        # `on_shutdown` handlers, so a child releasing work against a shared
        # resource tears down while that resource is still open.
        self._started_subapps: list[LifecycleMixin] = []
        # Strong references, so the loop cannot GC an in-flight task.
        self._spawned_named: dict[str, asyncio.Task[Any]] = {}
        self._spawned_anon: set[asyncio.Task[Any]] = set()

    def _init_middleware_state(self) -> None:
        """Set up the app logger and the ledger middleware registration writes to."""
        self.logger = logging.getLogger(self.import_name)

        self._middlewares: list[Middleware] = []
        # `(priority, sequence, instance)`, `sequence` being a monotonic counter
        # so equal priorities keep registration order. While `_any_priority` is
        # False, `_middlewares` is exactly the append-order list and the sort is
        # skipped; once a priority is set it is sorted at registration time.
        self._middleware_records: list[tuple[int, int, Middleware]] = []
        self._middleware_seq = 0
        self._any_priority = False
        # A route's exclusion-chain cache (`RouteInfo._mw_chain_cache`) keys on
        # this, so a filtered chain is recomputed only when the set changes.
        self._mw_version = 0

    def _init_feature_pipeline(self) -> None:
        """Set up the feature registry and the pipeline compiled from it.

        The `FeatureSpec` declarations are appended in order and read back as a
        sequence, so they stay together here rather than being split by topic.
        """
        # `_pipeline` is rebuilt lazily when `cp.gen != self._gen`.
        self._gen = 0
        self._features: list[FeatureSpec] = []
        self._pipeline: CompiledPipeline | None = None
        # Handshake host / origin gate, pre-filtered at compile time so the
        # per-connect path iterates a tuple instead of probing every middleware.
        self._features.append(
            FeatureSpec(
                "ws.handshake",
                PH_WS_HANDSHAKE,
                enabled=lambda: bool(self._middlewares),
                build=lambda: build_ws_handshake_checks(self),
            )
        )
        # The `process_response` methods in reversed order, fused once so no
        # per-response `reversed()` alloc runs. Skipped for an excluding route.
        self._features.append(
            FeatureSpec(
                "http.middleware.response",
                PH_HTTP_POST,
                enabled=lambda: bool(self._middlewares),
                build=lambda: build_response_middleware(self),
            )
        )
        # The `process_request` methods in forward order, on the same terms.
        self._features.append(
            FeatureSpec(
                "http.middleware.request",
                PH_HTTP_PRE,
                enabled=lambda: bool(self._middlewares),
                build=lambda: build_request_middleware(self),
            )
        )
        # The `@app.middleware("http")` call_next chain. The slot is `None` when
        # none are registered, so `handle_request` skips the around branch.
        self._features.append(
            FeatureSpec(
                "http.middleware.around",
                PH_HTTP_AROUND,
                enabled=lambda: bool(self._http_middleware_funcs),
                build=lambda: tuple(self._http_middleware_funcs),
            )
        )
        # `None` for an un-instrumented app, which then never reads the perf
        # clock or runs the post-response metrics call.
        self._features.append(
            FeatureSpec(
                "http.instrumentation",
                PH_HTTP_FINISH,
                enabled=lambda: bool(self._instrumentation),
                build=lambda: tuple(self._instrumentation),
            )
        )
        # At the default `order`, so a higher-order wrapper (the live-otel span)
        # sorts ahead of it and ends up outermost.
        self._features.append(
            FeatureSpec(
                "asgi.middleware",
                PH_ASGI_WRAP,
                enabled=lambda: bool(self._asgi_middleware),
                build=lambda: list(self._asgi_middleware),
            )
        )

    def _init_asgi_stack_state(self) -> None:
        """Set up the ASGI wrapper stack and the observability hooks around it."""
        # Each instantiated as `cls(app, **options)` on the first request.
        self._asgi_middleware: list[tuple[Any, dict[str, Any]]] = []
        # Rebuilt when the compiled pipeline's generation advances, so no wrapper
        # registry needs its own manual stack reset.
        self._asgi_stack: Callable[..., Any] | None = None
        self._asgi_stack_gen: int = -1
        self._instrumentation: list[Callable[..., Any]] = []
        # Sparse on purpose: empty, the dispatch loop skips the membership test.
        self._instrumentation_excludes: dict[Callable[..., Any], frozenset[str]] = {}

    def _init_introspection_caches(self) -> None:
        """Set up the watchdog handle, error-handler maps and lazy route caches."""
        # Armed during startup only when `EVENT_LOOP_WATCHDOG` is set.
        self._watchdog: Any = None
        self._exception_handlers: dict[type[Any], Callable[..., Any]] = {}
        self._status_handlers: dict[int, Callable[..., Any]] = {}
        self._cached_routes = None
        self._cached_view_functions = None
        self._cached_url_map = None
        # Cached `_find_exception_handler` MRO walks. Unbounded in principle; an
        # app raising a fixed set of exception classes keeps it to a few dozen.
        self._exc_handler_cache: dict[type[Any], Callable[..., Any] | None] = {}

    def _init_registries(self) -> None:
        """Initialise the per-app hook, mount, template, and URL registries.

        Arg-free; called once from `__init__` after the constructor-time
        handler and middleware registration.
        """
        self._on_startup: list[Callable[..., Any]] = []
        self._on_shutdown: list[Callable[..., Any]] = []
        self._static_handlers: list[StaticFiles] = []
        self._dependency_overrides: dict[Callable[..., Any], Callable[..., Any]] = {}
        # `(sub_plan, is_coro, is_gen, is_async_gen)` per overridden dependency,
        # so each request's fresh `DependencyResolver` does not pay the build +
        # triple-probe cost. Weakly keyed: a transient target must not be pinned.
        self._override_subplans: weakref.WeakKeyDictionary[Callable[..., Any], Any] = (
            weakref.WeakKeyDictionary()
        )
        self._before_request_hooks: list[Callable[..., Any]] = []
        self._before_first_request_hooks: list[Callable[..., Any]] = []
        # The lock stops two concurrent first requests from both seeing
        # `_first_request_fired = False`. Allocated lazily so it binds to the
        # running loop - at construction, at module scope, there is none.
        self._first_request_fired = False
        self._first_request_lock: asyncio.Lock | None = None
        self._after_request_hooks: list[Callable[..., Any]] = []
        self._teardown_request_hooks: list[Callable[..., Any]] = []
        # Bucketed by name: dispatch walks only the bucket matching the matched
        # route's `endpoint` prefix, not every blueprint's gate checks.
        self._bp_before_hooks: dict[str, list[Callable[..., Any]]] = {}
        self._bp_after_hooks: dict[str, list[Callable[..., Any]]] = {}
        self._bp_teardown_hooks: dict[str, list[Callable[..., Any]]] = {}
        # Kept out of the app-global tables so a blueprint's handler catches only
        # its own (or a nested descendant's) routes, never a sibling's.
        self._bp_exception_handlers: dict[str, dict[type[Any], Callable[..., Any]]] = {}
        self._bp_status_handlers: dict[str, dict[int, Callable[..., Any]]] = {}
        self._teardown_appcontext_hooks: list[Callable[..., Any]] = []
        self._context_processors: list[Callable[..., Any]] = []
        # `(prefix, prefix + "/", sub_app)`; the second slot is the boundary the
        # dispatcher compares the path against, so the loop does not rebuild it.
        self._mounted_apps: list[tuple[str, str, Any]] = []
        # Same shape for ASGI-layer mounts dispatched with the raw scope.
        self._asgi_mounts: list[tuple[str, str, Any]] = []
        # `expose_mcp=True` sub-apps, published through this app's own MCP server.
        self._mcp_mounts: list[tuple[str, Any]] = []
        # Built elsewhere, not derived from a signature, so added as they are.
        self._mcp_prebuilt_tools: list[Any] = []
        self._http_middleware_funcs: list[Callable[..., Any]] = []  # @app.middleware("http") funcs
        # Jinja2 helper registrations - applied to the env on each render.
        self._template_filters: list[tuple[str, Callable[..., Any]]] = []
        self._template_globals: list[tuple[str, Callable[..., Any]]] = []
        self._template_tests: list[tuple[str, Callable[..., Any]]] = []
        # Non-middleware components that report to `veloce check`.
        self._auditables: list[Any] = []
        # A preprocessor runs after route match and can mutate `path_params`; a
        # url_defaults function runs inside `url_for` and injects default kwargs.
        self._url_value_preprocessors: list[Callable[..., Any]] = []
        self._url_default_funcs: list[Callable[..., Any]] = []
        # Bucketed by dotted blueprint name as the request hooks are.
        self._bp_url_value_preprocessors: dict[str, list[Callable[..., Any]]] = {}
        self._bp_url_default_funcs: dict[str, list[Callable[..., Any]]] = {}
        # `(error, endpoint, values)` callbacks, when `url_for` cannot build a URL.
        self.url_build_error_handlers: list[Callable[..., Any]] = []
        self._blueprints_map: dict[str, Any] = {}
        # Each returns a dict merged into `veloce shell`'s namespace.
        self._shell_context_processors: list[Callable[..., Any]] = []
        self._cli_group = None
        # Routes registered for the OpenAPI 3.1 `webhooks` section, never dispatched.
        self.webhooks = Blueprint("webhooks")
        from veloce.json_provider import DefaultJSONProvider

        self.json_provider_class: Any = DefaultJSONProvider
        self._json_provider = None
        # Resolved on first use. `None` means the default provider with nothing
        # configured, which is byte-for-byte what the direct orjson path emits.
        self._handler_json_dumps: Any = _UNRESOLVED_JSON_DUMPS
        # Built on first `app.aborter` access, so a subclass can replace it first.
        self._aborter = None
        # Resolved relative to `package_root` when not absolute; mounting a
        # `StaticFiles` handler at `static_url_path` is opt-in.
        self.static_folder: str = "static"
        self.static_url_path: str = "/static"
