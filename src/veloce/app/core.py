"""Veloce application — the main entry point."""

from __future__ import annotations

import functools
import inspect
import os
import sys
import threading
import warnings
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from veloce._internal import (
    _coerce_bool,
)
from veloce._pipeline import (
    CompiledPipeline,
    compile_pipeline,
)
from veloce.app.accessors import AccessorsMixin
from veloce.app.asgi import AsgiMixin
from veloce.app.background import BackgroundTasksMixin
from veloce.app.construction import ConstructionMixin
from veloce.app.dispatch import DispatchMixin
from veloce.app.errors import ErrorsMixin
from veloce.app.introspection import IntrospectionMixin
from veloce.app.lifecycle import LifecycleMixin
from veloce.app.mcp import MCPMixin
from veloce.app.middleware import MiddlewareMixin
from veloce.app.mounting import MountingMixin
from veloce.app.openapi import OpenAPIMixin
from veloce.app.plugins import PluginsMixin
from veloce.app.posture import PostureMixin
from veloce.app.registration import RegistrationMixin
from veloce.app.serving import ServingMixin
from veloce.app.templating import TemplatingMixin
from veloce.app.testing import TestingMixin
from veloce.app.urls import URLMap
from veloce.blueprints import Blueprint
from veloce.exceptions import (
    SetupError,
)
from veloce.http.datastructures import State
from veloce.routing.route_info import RouteHandler, RouteOptions
from veloce.routing.router import Router

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.icons import Icon
    from veloce.settings import Settings


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
    edit away from a real parameter name almost certainly is. The names are
    cached - introspecting the 35-parameter `Annotated` signature costs ~140us.
    """
    import difflib

    known = _constructor_parameter_names()
    for key in extra:
        # A ratio is `2M / (len(a) + len(b))`, so 0.8 is unreachable once one
        # name is half again as long as the other.
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
    AccessorsMixin,
    AsgiMixin,
    ConstructionMixin,
    DispatchMixin,
    ErrorsMixin,
    IntrospectionMixin,
    LifecycleMixin,
    MCPMixin,
    MiddlewareMixin,
    MountingMixin,
    OpenAPIMixin,
    PluginsMixin,
    PostureMixin,
    RegistrationMixin,
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

    # The emitted version string only: the schema objects are JSON Schema
    # 2020-12, which a 3.0 value here would misdeclare.
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
        *,
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
        # Refused, not resolved against the launch directory. A leading
        # separator counts as rooted: Windows calls `/srv/app` relative.
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
        # Both REQUIRED strings (OpenAPI 3.1 Sec. 4.8.2) and both interpolated.
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
        # Registration is not atomic, and the flag above is what another loop
        # reads to decide it has finished.
        self._openapi_setup_guard = threading.Lock()
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
    def settings(self) -> Settings:
        """Typed, read-only snapshot of `app.config`, rebuilt on each access."""
        from veloce.settings import Settings

        return Settings.from_config(self.config)

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

        Iterating yields `URLRule` objects (rule, methods, endpoint),
        subscripting by endpoint name returns that endpoint's rules, and `len()`
        is the total route count.
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

    def _add_route_options(
        self, path: str, handler: RouteHandler, methods: list[str], options: RouteOptions
    ) -> None:
        """Register a route, bracketed by the mutability check and cache reset.

        Refuse a mutation once the app is serving, then drop the caches the new
        route invalidates. Every registration path (`add_route`, `route`,
        blueprint splicing) reaches the base implementation through here.
        """
        self._assert_mutable()
        super()._add_route_options(path, handler, methods, options)
        self._invalidate_route_caches()

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
        `Router.include_router`. `url_prefix` wins when both are given.
        """
        self._assert_mutable()

        effective = url_prefix if url_prefix is not None else (prefix or None)
        if isinstance(router, Blueprint):
            self.register_blueprint(router, url_prefix=effective)
        else:
            Router.include_router(self, router, prefix=effective or "")
            self._invalidate_route_caches()

    # ── Security posture ───────────────────────────────────

    # ── Signals, aborter, and CLI ──────────────────────────

    # ── Blueprints and URL rules ───────────────────────────

    # ── Dependency overrides (for testing) ────────────────

    # ── Construction helpers ──────────────────────────────
