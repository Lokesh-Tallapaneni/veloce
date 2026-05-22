"""Veloce application — the main entry point."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import signal
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from veloce.contrib.staticfiles import StaticFiles
from veloce.dependency import DependencyResolver, Depends
from veloce.exceptions import (
    HTTPException,
    RequestValidationError,
    WebSocketException,
    WebSocketRequestValidationError,
)
from veloce.http.request import Request
from veloce.http.response import (
    JSONResponse,
    Response,
    _reject_header_crlf,
)
from veloce.middleware import BaseHTTPMiddleware, Middleware
from veloce.routing.router import Router

if TYPE_CHECKING:
    import ssl


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
        # App-level `dependencies` / `responses` — applied
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
        # instance folder — explicit override, else computed from
        # `package_root` on first `instance_path` access.
        self._instance_path = instance_path
        # `import_name` — defaults to the caller's module so
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
        # OpenAPI 3.1 §4.8.2 `info.summary` — a short one-line summary
        # of the API, distinct from the longer `description`.
        self.summary = summary
        self.debug = debug
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

        # App-wide scratch namespace. `State` is a dict
        # subclass, so `app.state["db"]` and `app.state.db` both work.
        from veloce.http.request import State

        self.state: State = State()
        # Configuration. `Config` is a dict subclass with
        # loader methods (from_object, from_pyfile, from_mapping, …).
        # Seeded with the documented default keys so `app.config[k]`
        # returns a value rather than raising `KeyError`.
        self.config: Config = Config(Config.default_config())
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
        # Standard ASGI middleware — `(class, options)` pairs. Each wraps the
        # whole ASGI application (instantiated as `cls(app, **options)`) and
        # is assembled lazily into `_asgi_stack` on the first request.
        self._asgi_middleware: list[tuple[Any, dict[str, Any]]] = []
        self._asgi_stack: Callable | None = None
        # Observability instrumentation hooks — each is invoked once per
        # finished HTTP request with a `RequestMetrics` record. Empty by
        # default, so an un-instrumented app pays nothing.
        self._instrumentation: list[Callable] = []
        # Dev-mode event-loop blocking watchdog — armed during startup only
        # when the `EVENT_LOOP_WATCHDOG` config key is set, so it is `None`
        # (and free) for every other app.
        self._watchdog: Any = None
        self._exception_handlers: dict[type, Callable] = {}
        self._status_handlers: dict[int, Callable] = {}
        # `exception_handlers=` ctor mapping — keys are
        # exception classes or integer status codes.
        for _key, _handler in (exception_handlers or {}).items():
            self.add_exception_handler(_key, _handler)
        # ASGI shape `middleware=` ctor list — each entry is
        # a middleware instance applied in the given order.
        for _mw in middleware or []:
            self.add_middleware(_mw)
        self._on_startup: list[Callable] = []
        self._on_shutdown: list[Callable] = []
        self._static_handlers: list[StaticFiles] = []
        self._dependency_resolver = DependencyResolver()
        self._dependency_overrides: dict[Callable, Callable] = {}
        self._before_request_hooks: list[Callable] = []
        self._before_first_request_hooks: list[Callable] = []
        # Single-fire guard: lock prevents concurrent first requests from
        # both seeing `_first_request_fired = False` and running hooks twice.
        self._first_request_fired = False
        self._first_request_lock = asyncio.Lock()
        self._after_request_hooks: list[Callable] = []
        self._teardown_request_hooks: list[Callable] = []
        self._teardown_appcontext_hooks: list[Callable] = []
        self._context_processors: list[Callable] = []
        self._mounted_apps: list[tuple[str, Any]] = []
        # Arbitrary ASGI apps mounted at a prefix — dispatched at the ASGI
        # layer with the raw scope, distinct from veloce sub-apps above.
        self._asgi_mounts: list[tuple[str, Any]] = []
        self._http_middleware_funcs: list[Callable] = []  # @app.middleware("http") funcs
        # Jinja2 helper registrations — applied to the env on each render.
        self._template_filters: list[tuple[str, Callable]] = []
        self._template_globals: list[tuple[str, Callable]] = []
        self._template_tests: list[tuple[str, Callable]] = []
        # URL processors: preprocessor runs after route match and
        # can mutate path_params (e.g. pop a lang segment into g); url_defaults
        # runs inside url_for/url_path_for and can inject default kwargs.
        self._url_value_preprocessors: list[Callable] = []
        self._url_default_funcs: list[Callable] = []
        # `url_build_error_handlers` — list of `(error, endpoint, values)`
        # callbacks consulted when `url_for` can't build a URL.
        self.url_build_error_handlers: list[Callable] = []
        # `app.blueprints` view + `iter_blueprints()` iterator —
        # name → Blueprint of every successfully registered blueprint.
        self._blueprints_map: dict[str, Any] = {}
        # `@app.shell_context_processor` registry — each function
        # returns a dict that's merged into `veloce shell`'s namespace.
        self._shell_context_processors: list[Callable] = []
        # Lazily-built `click.Group` for app-defined CLI commands. Built
        # on first `app.cli` access so `click` isn't a hard import.
        self._cli_group: Any = None
        # `app.webhooks` — an APIRouter whose routes are pure
        # documentation: registered for the OpenAPI 3.1 `webhooks`
        # section, never dispatched.
        from veloce.blueprints import Blueprint

        self.webhooks = Blueprint("webhooks")
        # JSON provider — the. Class attribute is overridable;
        # instance is built lazily on first `app.json` access.
        from veloce.json_provider import DefaultJSONProvider

        self.json_provider_class: Any = DefaultJSONProvider
        self._json_provider: Any = None
        # Callable `Aborter`. Lazily built on first `app.aborter`
        # access so subclasses can override before use without paying
        # construction cost for apps that don't touch it.
        self._aborter: Any = None
        # Static-folder attributes — `static_folder` is
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

    # ── Middleware ────────────────────────────────────────────────

    # ── Properties ─────────────────────────────────────────────

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
        return _URLMap(self)

    @property
    def routes(self) -> list[dict[str, Any]]:
        """List all registered routes."""
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
        return result

    # ── Middleware ────────────────────────────────────────────────

    def add_middleware(self, middleware: Any, **options: Any) -> None:
        """Add middleware to the pipeline.

        Call forms:

        - `add_middleware(VeloceMiddlewareClass, **options)` — a class
          subclassing `Middleware` is instantiated with `**options` and
          appended to the request/response pipeline.
        - `add_middleware(instance)` — append an already-built `Middleware`
          instance directly.
        - `add_middleware(ASGIMiddlewareClass, **options)` — a class that
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
                # an ASGI app — registering it as ASGI would wire the app
                # in as its `dispatch` and fail at request time.
                raise TypeError(
                    f"{middleware.__name__} is a BaseHTTPMiddleware "
                    "(dispatch-shape) — register it with add_http_middleware(), "
                    "not add_middleware()."
                )
            else:
                # A standard ASGI middleware class — it needs the app it
                # wraps, so defer construction until the stack is built.
                self._asgi_middleware.append((middleware, options))
                self._asgi_stack = None
        elif isinstance(middleware, Middleware):
            self._middlewares.append(middleware)
        else:
            # A bare ASGI middleware instance cannot be wired up — veloce
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
        `RequestMetrics` record — the request method, the concrete path,
        the matched route *template* (a low-cardinality metric label), the
        status code, and the wall-clock duration in milliseconds. It may be
        a plain function or a coroutine function. A hook that raises is
        logged and skipped, so instrumentation never breaks a response.

        Returns `hook` unchanged, so it also works as a decorator:

            @app.add_instrumentation
            def export(metrics):
                statsd.timing(metrics.route or "unmatched", metrics.duration_ms)

        With no hook registered the request path carries no instrumentation
        cost — not even a clock read.
        """
        self._instrumentation.append(hook)
        return hook

    def use_secure_defaults(self) -> None:
        """Apply a security-hardened configuration baseline.

        - Marks the session cookie `Secure`, `HttpOnly`, and (unless
          already configured) `SameSite=Lax`.
        - Registers `SecurityHeadersMiddleware` — `nosniff`, frame-deny,
          a referrer policy, and a one-year HSTS max-age — unless one is
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
        if self.debug or self.config.get("DEBUG"):
            warnings.append("DEBUG is enabled — disable it before deploying to production.")
        if not self.config.get("SECRET_KEY"):
            warnings.append("SECRET_KEY is not set — session signing falls back to weak defaults.")
        has_session = any(isinstance(m, SessionMiddleware) for m in self._middlewares)
        if has_session and not self.config.get("SESSION_COOKIE_SECURE"):
            warnings.append(
                "SESSION_COOKIE_SECURE is off — the session cookie can be sent over plain HTTP."
            )
        if not any(isinstance(m, SecurityHeadersMiddleware) for m in self._middlewares):
            warnings.append(
                "No SecurityHeadersMiddleware registered — responses ship without hardening "
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
        """
        import os

        from veloce.helpers import send_from_directory

        directory = self.static_folder
        if not os.path.isabs(directory):
            directory = os.path.join(self.package_root, directory)
        return send_from_directory(directory, filename)

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
                "no Jinja environment — pass `template_folder=` to Veloce(...) "
                "or bind a Jinja2Templates instance first"
            )
        return self._templates.env

    @property
    def jinja_loader(self) -> Any:
        """The app's Jinja template loader.

        The `FileSystemLoader` (or whatever loader the bound
        `Jinja2Templates` env uses). `None` when no templating is
        configured — Veloce returns `None` for an app with no template
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
        this computed default. The directory is *not* auto-created —
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
        attribute returns the module — `app.signal_namespace.request_started`
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
        from veloce.exceptions import Aborter

        if self._aborter is None:
            self._aborter = Aborter()
        return self._aborter

    @aborter.setter
    def aborter(self, value: Any) -> None:
        self._aborter = value

    @property
    def got_first_request(self) -> bool:
        """`True` after the first request has been fully handled.

        compatibility — read-only. Useful when conditional setup
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
        time — the `ImportError` is deferred and produces a useful
        message instead of a hard-import crash on environments that
        don't need the CLI.
        """
        if getattr(self, "_cli_group", None) is None:
            try:
                import click
            except ImportError as err:  # pragma: no cover
                raise RuntimeError(
                    "app.cli requires `click` — install with: pip install click"
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
    # — which `_dispatch_request` already does inline — so both names
    # point at the same method.
    def dispatch_request(self, request: Request) -> Any:
        """an alias for `_dispatch_request`."""
        return self._dispatch_request(request)

    def full_dispatch_request(self, request: Request) -> Any:
        """an alias for `_dispatch_request` (which already runs the
        full before/after-request hook chain inline)."""
        return self._dispatch_request(request)

    async def preprocess_request(self, request: Request) -> Any:
        """Run all `before_request` hooks for `request`.

        Walks the registered hooks in order; if any hook returns a
        non-None value it short-circuits the chain and that value is
        returned (the contract — a non-None return becomes the
        response). Both sync and async hooks are supported.
        """
        for hook in self._before_request_hooks:
            result = await self._call_handler(hook, {"request": request})
            if result is not None:
                return result
        return None

    async def process_response(self, request: Request, response: Any) -> Any:
        """Run all `after_request` hooks for `(request, response)`.

        Hooks fire in **reverse** registration order; each hook may
        return a replacement response (the contract: a None return
        keeps the existing response).
        """
        for hook in reversed(self._after_request_hooks):
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
        if not inspect.iscoroutinefunction(func):
            return func

        @functools.wraps(func)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return asyncio.run(func(*args, **kwargs))

        return _sync_wrapper

    @staticmethod
    def async_to_sync(func: Callable) -> Callable:
        """Force-wrap `func` (sync or async) into a synchronous callable.

        Unlike `ensure_sync` this always returns a sync wrapper —
        useful when the caller needs a uniform `(*a, **kw) -> result`
        shape regardless of `func`'s coroutinicity.
        """
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            def _wrap(*args: Any, **kwargs: Any) -> Any:
                return asyncio.run(func(*args, **kwargs))

            return _wrap
        return func

    def make_response(self, value: Any) -> Response:
        """Coerce a handler-return value into a `Response`.

        Accepts (with this coercion table):
        - `Response` → returned as-is
        - `str` / `bytes` → wrapped as a text/HTML response
        - `dict` / `list` → wrapped as a JSON response via `jsonify`
        - `tuple` of `(body,)`, `(body, status)`, `(body, status, headers)`,
          or `(body, headers)` → unpacked and re-coerced
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
            return Response(body=value, content_type="text/html; charset=utf-8")
        if isinstance(value, str):
            return Response(
                body=value.encode("utf-8"),
                content_type="text/html; charset=utf-8",
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
        method: str = "GET",
        headers: dict[str, str] | None = None,
        query_string: str = "",
        body: bytes = b"",
    ) -> _TestRequestContext:
        """Synthesise a fake request for outside-request testing.

        Inside `with app.test_request_context(): ...`, `current_app`, `g`,
        and the request-scoped contextvars resolve as if Veloce
        had just received that request — without spinning up the full
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
        # Class → instance.
        if isinstance(middleware, type):
            middleware = middleware()
        if not callable(middleware):
            raise TypeError(
                f"add_http_middleware expects a callable / instance / class, got {middleware!r}"
            )
        self._http_middleware_funcs.append(middleware)
        return middleware

    def middleware(self, middleware_class_or_type: type | str, **kwargs) -> Any:
        """Add middleware — supports both a class form and a decorator form.

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
            assert not isinstance(middleware_class_or_type, str)
            self.add_middleware(middleware_class_or_type(**kwargs))

    # ── Exception handlers ───────────────────────────────────────

    def register_error_handler(self, code_or_exception: int | type, func: Callable) -> None:
        """Register an error handler without a decorator."""
        if isinstance(code_or_exception, int):
            self._status_handlers[code_or_exception] = func
        else:
            self._exception_handlers[code_or_exception] = func

    def _should_propagate_exceptions(self) -> bool:
        """Whether unhandled exceptions should re-raise out of dispatch.

        True when `app.config["PROPAGATE_EXCEPTIONS"]` is explicitly set,
        or implicitly when both DEBUG and TESTING are enabled.
        """
        explicit = self.config.get("PROPAGATE_EXCEPTIONS")
        if explicit is not None:
            return bool(explicit)
        return bool(self.config.get("DEBUG")) and bool(self.config.get("TESTING"))

    def _find_exception_handler(self, exc_type: type) -> Callable | None:
        """Walk `exc_type`'s MRO looking for a registered handler.

        Handlers registered against a base class catch every subclass —
        e.g. `@app.exception_handler(HTTPException)` catches every
        `NotFound`, `Forbidden`, etc. raised through `abort()`.
        """
        for cls in exc_type.__mro__:
            handler = self._exception_handlers.get(cls)
            if handler is not None:
                return handler
        return None

    def exception_handler(self, exc_class_or_status: type | int) -> Callable:
        """Register a custom exception handler by exception type or status code."""

        def decorator(func: Callable) -> Callable:
            if isinstance(exc_class_or_status, int):
                self._status_handlers[exc_class_or_status] = func
            else:
                self._exception_handlers[exc_class_or_status] = func
            return func

        return decorator

    # Veloce names this `errorhandler` (one word, no underscore). The
    # alias keeps calling code readable; semantics are identical.
    errorhandler = exception_handler

    def add_exception_handler(self, exc_class_or_status: type | int, handler: Callable) -> None:
        """Imperative exception-handler registration — ASGI shape.

        The non-decorator form of `@app.exception_handler(...)`.
        Accepts an exception class (matched by MRO at dispatch time) or
        an int HTTP status code.
        """
        if isinstance(exc_class_or_status, int):
            self._status_handlers[exc_class_or_status] = handler
        else:
            self._exception_handlers[exc_class_or_status] = handler

    def log_exception(self, exc: BaseException) -> None:
        """Log an exception with traceback.

        Routes the exception through the app logger at ERROR level.
        Used internally before falling back to a 500 response; exposed
        publicly so error-handler code can re-log via the same path.
        """
        self.logger.error("Exception on request", exc_info=exc)

    async def handle_http_exception(self, exc: HTTPException) -> Response:
        """Build the response for an `HTTPException`.

        Walks registered status-code + class handlers first (matching
        `abort()` semantics), falling back to JSON `{"detail": exc.detail}`
        with `exc.headers` applied. Useful for code paths outside the
        normal request cycle (e.g. background tasks) that want
        framework-consistent error shapes.
        """
        handler = self._status_handlers.get(exc.status_code) or self._find_exception_handler(
            type(exc)
        )
        if handler is not None:
            from veloce.http.request import Request as _Req

            req = _Req(method="GET", path="/", query_string="", headers={}, body=b"")
            result = await self._call_exc_handler(handler, req, exc)
            if isinstance(result, Response):
                return result
            return self._coerce_response(result)
        structured = getattr(exc, "errors", None)
        return JSONResponse(
            {"detail": structured if structured is not None else (exc.detail or "Error")},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    def make_default_options_response(self, path: str) -> Response:
        """Build the auto-OPTIONS response for `path`.

        Returns a 200 response with an empty body and an `Allow` header
        listing every method registered for `path`, augmented with
        `HEAD` (whenever `GET` is supported) and `OPTIONS` itself per
        RFC 9110 §9.3.7. Callers that register an explicit OPTIONS
        handler can use this to compose the default `Allow` set.
        """
        allowed = self.get_allowed_methods(path)
        advertised = list(allowed)
        if "GET" in advertised and "HEAD" not in advertised:
            advertised.append("HEAD")
        if "OPTIONS" not in advertised:
            advertised.append("OPTIONS")
        return Response(
            status_code=200,
            body=b"",
            content_type="text/plain",
            headers={"Allow": ", ".join(advertised)},
        )

    def trap_http_exception(self, exc: BaseException) -> bool:
        """Decide whether an `HTTPException` should propagate.

        Returns True iff the exception should be re-raised (skipping
        the configured `errorhandler`) for a debugger to see. Honours:

        - `TRAP_HTTP_EXCEPTIONS = True` — trap every `HTTPException`.
        - `TRAP_BAD_REQUEST_ERRORS = True` (default in debug mode) —
          trap only `BadRequest`/`Unauthorized`/`Forbidden`/`NotFound`
          style 4xx errors so unexpected 404s/400s surface during
          development. Non-HTTP exceptions are never trapped here.
        """
        if not isinstance(exc, HTTPException):
            return False
        if self.config.get("TRAP_HTTP_EXCEPTIONS"):
            return True
        trap_bad_request = self.config.get("TRAP_BAD_REQUEST_ERRORS")
        if trap_bad_request is None and self.debug:
            trap_bad_request = True
        return bool(trap_bad_request) and 400 <= (exc.status_code or 0) < 500

    async def handle_user_exception(self, exc: BaseException) -> Response:
        """Dispatch an arbitrary exception.

        `HTTPException` → `handle_http_exception`. Otherwise walks
        registered class handlers (MRO); on no match, logs via
        `log_exception` and returns 500.
        """
        if isinstance(exc, HTTPException):
            return await self.handle_http_exception(exc)
        handler = self._find_exception_handler(type(exc))
        if handler is not None:
            from veloce.http.request import Request as _Req

            req = _Req(method="GET", path="/", query_string="", headers={}, body=b"")
            result = await self._call_exc_handler(handler, req, exc)
            if isinstance(result, Response):
                return result
            return self._coerce_response(result)
        self.log_exception(exc)
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)

    @property
    def view_functions(self) -> dict[str, Callable]:
        """A `{endpoint_name: handler}` view of registered routes.

        Endpoint names follow a simple rule — the route's `name=`
        kwarg, or the handler's `__name__` when no name is set; blueprint
        routes are prefixed with `<bpname>.`. Returned dict is a fresh
        snapshot — mutation doesn't poison framework state.
        """
        out: dict[str, Callable] = {}
        for _method, _path, info in self._collect_all_routes():
            out[info.name] = info.handler
        return out

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
                        from veloce._handler_plan import build_plan

                        info.handler_plan = build_plan(func)
                    except Exception:
                        info.handler_plan = None
            if not replaced:
                raise ValueError(f"No route registered for endpoint {name!r}")
            return func

        return decorator

    @property
    def error_handler_spec(self) -> dict[Any, dict[Any, Callable]]:
        """Inspection view of registered error handlers.

        Returns a `{blueprint_name_or_None: {key: handler}}` mapping.
        veloce keeps a flat registry (no per-blueprint sub-tables —
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

        Returns `{blueprint_name_or_None: [hook, ...]}`. Like
        `error_handler_spec`, veloce flattens blueprint hooks into the
        app's list at registration time, so the returned dict carries
        a single `None` key.
        """
        return {None: list(self._before_request_hooks)}

    @property
    def after_request_funcs(self) -> dict[Any, list[Callable]]:
        return {None: list(self._after_request_hooks)}

    @property
    def teardown_request_funcs(self) -> dict[Any, list[Callable]]:
        return {None: list(self._teardown_request_hooks)}

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

    # ── Before/After request hooks ─────────────────

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
        top, in registration order — later processors win on conflicts.
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

        A legacy hook style — lifespan startup handlers are preferred,
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

    def context_processor(self, func: Callable) -> Callable:
        """Register a template context processor.
        The function should return a dict that merges into the template context."""
        self._context_processors.append(func)
        return func

    # ── Jinja2 helper registration ────────────────────

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
        """Register a callable as a Jinja global — accessible from any
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
        """Register a Jinja test — used in `{% if x is name %}` constructs."""

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
        keys the caller already set (the documented semantics — explicit context
        wins). Returns the same dict for chaining.
        """
        for processor in self._context_processors:
            result = processor()
            if isinstance(result, dict):
                for k, v in result.items():
                    context.setdefault(k, v)
        return context

    # ── URL processors (URL hooks) ─────────────────────────────

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
            super().include_router(router, prefix=effective or "")

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
        prefixes — the blueprint itself stays unmodified.
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
            )

        # Wrap each blueprint hook with an endpoint-prefix gate so the
        # hook fires only for routes that belong to this blueprint.
        gate_prefix = f"{bp_name}."

        def _gate(hook: Callable) -> Callable:
            if inspect.iscoroutinefunction(hook):

                async def _gated_async(*args: Any, **kwargs: Any) -> Any:
                    req = args[0] if args else kwargs.get("request")
                    if req is None or not (req.endpoint or "").startswith(gate_prefix):
                        return None
                    return await hook(*args, **kwargs)

                return _gated_async

            def _gated_sync(*args: Any, **kwargs: Any) -> Any:
                req = args[0] if args else kwargs.get("request")
                if req is None or not (req.endpoint or "").startswith(gate_prefix):
                    return None
                return hook(*args, **kwargs)

            return _gated_sync

        for hook in blueprint._before_request_hooks:
            self._before_request_hooks.append(_gate(hook))
        for hook in blueprint._after_request_hooks:
            self._after_request_hooks.append(_gate(hook))
        for hook in blueprint._teardown_request_hooks:
            self._teardown_request_hooks.append(_gate(hook))

        # URL processors (L7) — wrapped so they only fire for endpoints
        # belonging to the blueprint. The endpoint string is the first
        # arg of the `(endpoint, values)` callable.
        def _proc_gate(fn: Callable) -> Callable:
            def _gated(endpoint: str, values: dict) -> Any:
                if endpoint and endpoint.startswith(gate_prefix):
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
                    f"endpoint {endpoint!r} has no view function yet — "
                    f"attach one with @app.endpoint({endpoint!r})"
                )

            _stub_view.__name__ = endpoint
            view_func = _stub_view
        self.add_route(
            path=rule,
            handler=view_func,
            methods=methods or ["GET"],
            name=endpoint,
            **kwargs,
        )

    # ── Dependency overrides (for testing) ────────────────────────

    def dependency_overrides_provider(self) -> dict[Callable, Callable]:
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

    # ── Mount sub-applications ────────────────────────────────────

    def mount(self, prefix: str, app: Any) -> None:
        """Mount a sub-application at a path prefix.

        A veloce sub-app is dispatched through the parent's request
        pipeline. Any other ASGI application — a Starlette app, an ASGI
        micro-app, an instrumentation shim — is dispatched at the ASGI
        layer instead: the matched prefix is stripped from the scope's
        `path` and moved onto `root_path`, so the mounted app sees a
        normal root-relative request.
        """
        prefix = prefix.rstrip("/")
        if isinstance(app, Veloce):
            self._mounted_apps.append((prefix, app))
        else:
            self._asgi_mounts.append((prefix, app))

    def _match_asgi_mount(self, path: str) -> tuple[str, Any] | None:
        """Return the `(prefix, app)` whose prefix owns `path`, if any."""
        for prefix, mounted in self._asgi_mounts:
            if path == prefix or path.startswith(prefix + "/"):
                return prefix, mounted
        return None

    # ── Lifecycle events ─────────────────────────────────────────

    def on_event(self, event: str) -> Callable:
        """Register startup/shutdown event handlers."""

        def decorator(func: Callable) -> Callable:
            if event == "startup":
                self._on_startup.append(func)
            elif event == "shutdown":
                self._on_shutdown.append(func)
            return func

        return decorator

    def on_startup(self, func: Callable) -> Callable:
        self._on_startup.append(func)
        return func

    def on_shutdown(self, func: Callable) -> Callable:
        self._on_shutdown.append(func)
        return func

    def add_event_handler(self, event: str, func: Callable) -> None:
        """Imperative event-handler registration — ASGI shape.

        `app.add_event_handler("startup", fn)` is the non-decorator
        form of `@app.on_event("startup")`. `event` must be
        `"startup"` or `"shutdown"`.
        """
        if event == "startup":
            self._on_startup.append(func)
        elif event == "shutdown":
            self._on_shutdown.append(func)
        else:
            raise ValueError(f"event must be 'startup' or 'shutdown', got {event!r}")

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

    # ── Static files ─────────────────────────────────────────────

    def mount_static(
        self,
        prefix: str = "/static",
        directory: str = "static",
        html: bool = False,
    ) -> None:
        """Mount a static file directory."""
        self._static_handlers.append(StaticFiles(directory=directory, prefix=prefix, html=html))

    # ── Request handling ─────────────────────────────────────────

    async def handle_request(self, request: Request) -> Response:
        """Main request handler — runs middleware chain + route dispatch."""
        # Lazy OpenAPI setup (ensures routes exist on first request regardless of entry point)
        self._setup_openapi()

        # Inject app reference into request
        request.app = self

        # Bind `current_app` for the duration of this request. Uses
        # `_current_app_var` directly (not `set/reset`) because veloce's
        # async dispatch may span tasks that diverge from the binding's
        # token — letting the contextvar fall through naturally when
        # the request task ends.
        from veloce.helpers import _current_app_var, _current_request_var, g

        _current_app_var.set(self)
        # Also bind the request so module-level helpers like
        # `after_this_request()` can find it.
        _current_request_var.set(request)

        # Reset request-scoped globals (g object)
        g._reset()

        # Signal: request started. The finished/teardown
        # signals are imported lazily where they're fired so this hot
        # path doesn't carry references it doesn't use.
        from veloce.signals import request_finished, request_started

        if request_started.has_receivers_for(self):
            request_started.send(self, request=request)

        # Drain `before_first_request` hooks exactly once. The double-check
        # under the lock is the canonical pattern: the unlocked check
        # short-circuits the common (already-fired) case without acquiring
        # the lock; the locked check guarantees single-fire when concurrent
        # first requests race.
        if not self._first_request_fired and self._before_first_request_hooks:
            async with self._first_request_lock:
                if not self._first_request_fired:
                    for hook in self._before_first_request_hooks:
                        await self._call_handler(hook, {})
                    self._first_request_fired = True

        # Enforce MAX_CONTENT_LENGTH. Check both the declared
        # Content-Length (cheap reject) and the actually-buffered body size
        # (defence-in-depth when no Content-Length was sent). Per
        # RFC 9110 §15.5.14, the status is 413 Content Too Large.
        max_size = self.config.get("MAX_CONTENT_LENGTH")
        if max_size is not None:
            declared = request.content_length
            if (declared is not None and declared > max_size) or len(request.body) > max_size:
                return JSONResponse(
                    {
                        "detail": "Request body exceeds MAX_CONTENT_LENGTH",
                        "status_code": 413,
                        "limit": max_size,
                    },
                    status_code=413,
                )

        # Time the dispatch only when instrumentation hooks are registered —
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
                        request, 500, (time.perf_counter() - started) * 1000.0
                    )
            raise

        # Signal: request finished. Sender is the app, `response=` is the
        # final Response, `request=` lets a receiver correlate with the
        # matching `request_started`. Receivers may peek but not replace.
        if request_finished.has_receivers_for(self):
            try:
                request_finished.send(self, response=response, request=request)
            except Exception:
                self.logger.exception("request_finished signal raised an exception")

        if instrument:
            await self._run_instrumentation(
                request, response.status_code, (time.perf_counter() - started) * 1000.0
            )

        return response

    async def _run_http_middleware_chain(self, request: Request) -> Response:
        """Run @app.middleware('http') functions with call_next pattern."""
        idx = 0
        funcs = self._http_middleware_funcs

        async def call_next(req: Request) -> Response:
            nonlocal idx
            idx += 1
            if idx < len(funcs):
                return await funcs[idx](req, call_next)
            return await self._dispatch_request(req)

        return await funcs[0](request, call_next)

    async def _dispatch_request(self, request: Request) -> Response:
        """Core request dispatch — middleware, routing, handler execution."""
        _exc: Exception | None = None
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

            # Run before_request hooks
            for hook in self._before_request_hooks:
                result = await self._call_handler(hook, {"request": request})
                if result is not None:
                    return await self._run_response_middleware(
                        request, self._coerce_response(result)
                    )

            # Check mounted sub-apps
            for prefix, sub_app in self._mounted_apps:
                if request.path.startswith(prefix + "/") or request.path == prefix:
                    sub_path = request.path[len(prefix) :] or "/"
                    sub_request = Request(
                        method=request.method,
                        path=sub_path,
                        query_string=request.query_string,
                        headers=request.headers,
                        body=request.body,
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

            # Route matching — reuse the match taken above unless a
            # before_request hook rewrote the request path or method, in
            # which case the routing inputs changed and we must re-match.
            if request.path != _matched_path or request.method != _matched_method:
                match = self.match(request.method, request.path)

            # Subdomain constraint check — if the matched route declares a
            # `subdomain`, the request's host must be `{subdomain}.{SERVER_NAME}`.
            # Mismatch raises 404 directly (not 405, because
            # the path is reachable, just not from this host).
            # `subdomain="*"` accepts any non-empty subdomain.
            if (
                match is not None
                and match.route_info.subdomain is not None
                and not self._subdomain_matches(request, match.route_info.subdomain)
            ):
                raise HTTPException(404, "Not Found")

            # Host constraint check — the full `Host` header must equal
            # the route's declared `host` (case-insensitive, port-stripped).
            # Mismatch → 404 (the path is reachable, just not from this host).
            if match is not None and match.route_info.host is not None:
                req_host = (request.headers.get("host", "") or "").split(":", 1)[0]
                if req_host.lower() != match.route_info.host.lower():
                    raise HTTPException(404, "Not Found")

            # Redirect slashes (like common web frameworks): /users -> /users/ or vice versa
            if match is None and self.redirect_slashes:
                alt = (
                    request.path.rstrip("/")
                    if request.path.endswith("/") and request.path != "/"
                    else request.path + "/"
                )
                alt_match = self.match(request.method, alt)
                if alt_match is not None:
                    from veloce.http.response import RedirectResponse

                    code = 308 if request.method != "GET" else 307
                    return RedirectResponse(alt, status_code=code)

            if match is None:
                # Check if path exists but method is wrong
                allowed = self.get_allowed_methods(request.path)
                if allowed:
                    # RFC 9110 §9.3.7: OPTIONS auto-responds with `Allow:` and
                    # an empty body even when no handler is registered.
                    if request.method == "OPTIONS":
                        return self.make_default_options_response(request.path)
                    return await self._handle_error(
                        request,
                        405,
                        JSONResponse(
                            {"detail": "Method Not Allowed", "allowed": allowed},
                            status_code=405,
                            headers={"Allow": ", ".join(allowed)},
                        ),
                    )
                raise HTTPException(404, "Not Found")

            # Set path params + endpoint name on request.
            request.path_params = match.path_params
            # the routing-rule `defaults` — fill in fixed values for params
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

            # Resolve dependencies (with overrides) and call handler.
            # Fast path: consume the pre-built handler plan that Router.add_route
            # cached on RouteInfo at registration time.
            resolver = self._dependency_resolver
            resolver._overrides = self._dependency_overrides
            route_info = match.route_info
            if route_info.handler_plan is not None:
                if route_info.is_trivial_plan:
                    # Trivial-route executor: the handler takes no injected
                    # parameters and the route declares no dependencies, so
                    # the dependency subsystem has nothing to produce. Skip
                    # it entirely — just clear the shared resolver's
                    # per-request state — instead of awaiting a resolve that
                    # would return `{}`.
                    resolver.reset()
                    kwargs = {}
                else:
                    kwargs = await resolver.resolve_plan(
                        route_info.handler_plan,
                        request,
                        match.path_params,
                        route_info.route_dep_plans,
                    )
            else:
                kwargs = await resolver.resolve(
                    route_info.handler,
                    request,
                    match.path_params,
                    route_dependencies=[
                        d for d in route_info.dependencies if isinstance(d, Depends)
                    ],
                )

            result = await self._call_handler(
                route_info.handler,
                kwargs,
                is_coro=(
                    route_info.handler_plan.is_coro if route_info.handler_plan is not None else None
                ),
            )

            # Apply response_model validation + dump flags before coercion.
            # The handler may return a dict/BaseModel/list; if the route
            # declared a response_model, route the value through it so
            # extra fields drop, aliases apply, and unset/None filters fire.
            if match.route_info.response_model is not None and not isinstance(result, Response):
                result = self._apply_response_model(result, match.route_info)

            response = self._coerce_response(result, match.route_info.response_class)

            # Apply route-level status_code override
            if match.route_info.status_code != 200 and response.status_code == 200:
                response.status_code = match.route_info.status_code
                response._encoded = None

            # Response injection — merge a handler-injected
            # Response's status_code + headers onto the final response.
            # Skipped when the handler returned a Response itself (its own
            # status/headers already win). `status_code == 0` means the
            # handler never touched it, so it is not applied.
            injected = request._state.get("_injected_response") if request._state else None
            if injected is not None and not isinstance(result, Response):
                if injected.status_code:
                    response.status_code = injected.status_code
                for hk, hv in injected.headers.items():
                    if hk.lower() == "set-cookie" and "Set-Cookie" in response.headers:
                        response.headers["Set-Cookie"] = (
                            response.headers["Set-Cookie"] + "\r\nSet-Cookie: " + hv
                        )
                    else:
                        response.headers[hk] = hv
                response._encoded = None

            # Run after_request hooks
            for hook in self._after_request_hooks:
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
                    fn_result = await self._call_handler(
                        fn, {"request": request, "response": response}
                    )
                    if fn_result is not None and isinstance(fn_result, Response):
                        response = fn_result

            # Run background tasks if present — hold strong ref to prevent GC
            if request._background_tasks is not None:
                bg_task = asyncio.get_running_loop().create_task(
                    request._background_tasks.run_all()
                )
                bg_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

            # Response-attached background task (shape:
            # `Response(content=..., background=BackgroundTask(fn))`).
            # Runs in the same fire-and-forget pattern as the
            # DI-injected BackgroundTasks queue.
            attached_bg = getattr(response, "background", None)
            if attached_bg is not None:
                # `BackgroundTasks` collection → `.run_all()`;
                # single `BackgroundTask` → `.run()`. Anything else with
                # a `run()` coroutine method is supported too.
                if hasattr(attached_bg, "run_all"):
                    coro = attached_bg.run_all()
                elif hasattr(attached_bg, "run"):
                    coro = attached_bg.run()
                else:
                    coro = None
                if coro is not None:
                    bg_task = asyncio.get_running_loop().create_task(coro)
                    bg_task.add_done_callback(
                        lambda t: t.exception() if not t.cancelled() else None
                    )

            return await self._run_response_middleware(request, response)

        except HTTPException as exc:
            _exc = exc
            # Status-code handler wins over class handler; class handler walks
            # the MRO so e.g. registering on `HTTPException` catches `NotFound`.
            handler = self._status_handlers.get(exc.status_code) or self._find_exception_handler(
                type(exc)
            )
            if handler:
                result = await self._call_exc_handler(handler, request, exc)
                return self._coerce_response(result)

            # `ValidationError` / `RequestValidationError` carry a
            # structured `.errors` list — emit it verbatim (the
            # shape `{"detail": [ {loc, msg, type}, … ]}`) rather than
            # the stringified repr stored in `exc.detail`.
            structured = getattr(exc, "errors", None)
            detail_payload: Any = structured if structured is not None else exc.detail
            return await self._handle_error(
                request,
                exc.status_code,
                JSONResponse(
                    {"detail": detail_payload, "status_code": exc.status_code},
                    status_code=exc.status_code,
                    headers=exc.headers,
                ),
            )
        except Exception as exc:
            _exc = exc
            handler = self._find_exception_handler(type(exc))
            if handler:
                result = await self._call_exc_handler(handler, request, exc)
                return self._coerce_response(result)

            # PROPAGATE_EXCEPTIONS: when set (or implicitly
            # when both DEBUG and TESTING are on), let the exception
            # escape the handler. Test suites use this to see real
            # tracebacks instead of "Internal Server Error" responses.
            if self._should_propagate_exceptions():
                raise

            if self.debug:
                import traceback

                tb = traceback.format_exc()
                return Response(
                    status_code=500,
                    body=tb.encode(),
                    content_type="text/plain",
                )

            return await self._handle_error(
                request,
                500,
                JSONResponse({"detail": "Internal Server Error"}, status_code=500),
            )
        finally:
            # Yield-dependency teardowns first — they conceptually wrap the
            # request (the resource was acquired before the handler ran and
            # must be released regardless of outcome). Errors here are
            # swallowed inside `run_teardowns` so the response cycle stays
            # intact.
            try:
                await self._dependency_resolver.run_teardowns(_exc)
            except Exception:
                self.logger.exception("yield-dependency teardown raised")

            # Teardown hooks — always run, even on exceptions.
            for hook in self._teardown_request_hooks:
                try:
                    if inspect.iscoroutinefunction(hook):
                        await hook(_exc)
                    else:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, hook, _exc)
                except Exception:
                    self.logger.exception("teardown_request hook raised an exception")

            # `teardown_appcontext` fires when the app context pops; in
            # veloce that happens at the end of each request (no separate
            # app/request context split). Hooks receive the exception or
            # None. Errors are logged, never re-raised.
            for hook in self._teardown_appcontext_hooks:
                try:
                    if inspect.iscoroutinefunction(hook):
                        await hook(_exc)
                    else:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, hook, _exc)
                except Exception:
                    self.logger.exception("teardown_appcontext hook raised an exception")

            # Signals: fire `got_request_exception` first when an exc bubbled
            # up, then always fire `request_tearing_down`. Receivers may
            # raise — log + continue so a buggy listener doesn't poison
            # the dispatch path.
            from veloce.signals import got_request_exception, request_tearing_down

            try:
                if _exc is not None and got_request_exception.has_receivers_for(self):
                    got_request_exception.send(self, exception=_exc)
                if request_tearing_down.has_receivers_for(self):
                    request_tearing_down.send(self, exc=_exc)
            except Exception:
                self.logger.exception("signal receiver raised an exception")

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
        `"admin"`) — the request's `Host` header must be
        `{subdomain}.{SERVER_NAME}`. `"*"` matches any non-empty
        subdomain of `SERVER_NAME`. When no `SERVER_NAME` is configured
        we degrade to comparing the leftmost label of the host with
        the subdomain literal — useful for tests that drive the app
        without setting `SERVER_NAME`.
        """
        host = (request.host or "").split(":", 1)[0].lower()
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
        # No SERVER_NAME — compare the leftmost label.
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
        whether the handler is a coroutine — the handler plan precomputes
        it at registration — it passes `is_coro` to skip the per-request
        `inspect.iscoroutinefunction` probe.
        """
        if is_coro is None:
            is_coro = inspect.iscoroutinefunction(handler)
        if is_coro:
            return await handler(**kwargs)
        # Run sync handlers in executor to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(handler, **kwargs))

    async def _call_exc_handler(
        self, handler: Callable, request: Request, exc: BaseException
    ) -> Any:
        """Call an exception handler, adapting kwargs to match its signature."""
        sig = inspect.signature(handler)
        params = list(sig.parameters.keys())
        kwargs: dict[str, Any] = {}
        if "request" in params:
            kwargs["request"] = request
        if "exc" in params:
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

        from typing import get_args, get_origin

        origin = get_origin(model)
        # Sequence-style response models — `response_model=list[Item]` — dump
        # each element through the inner model.
        if origin is list:
            args = get_args(model)
            if args:
                inner = args[0]
                if not isinstance(result, (list, tuple)):
                    return result  # let downstream coercion handle the mismatch
                from pydantic import BaseModel as _BM

                if isinstance(inner, type) and issubclass(inner, _BM):
                    dumped: list[Any] = []
                    for item in result:
                        # Fast path: an element already of the target model
                        # is dumped directly — skipping a re-validation
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
        from pydantic import BaseModel as _BM

        if isinstance(model, type) and issubclass(model, _BM):
            # If the handler returned an instance of the target model, use
            # it directly — the dump-then-validate roundtrip would erase
            # the `__pydantic_fields_set__` info that drives
            # `exclude_unset`.
            if isinstance(result, model):
                return result.model_dump(**dump_kwargs)
            # Cross-model or dict input: dump any incoming BaseModel to a
            # dict first so model_validate can re-shape it. Cross-model
            # coercion (e.g. internal → public view) works as expected;
            # `exclude_unset` semantics necessarily reset because the
            # fields-set markers don't transfer across model types.
            payload = result.model_dump() if isinstance(result, _BM) else result
            validated = model.model_validate(payload)
            return validated.model_dump(**dump_kwargs)

        # Non-pydantic model (e.g. plain class) — pass through unchanged.
        return result

    def _coerce_response(self, result: Any, response_class: Any = None) -> Response:
        """Convert handler return value to a Response object."""
        if isinstance(result, Response):
            return result
        # Use custom response_class if specified
        if response_class is not None:
            if response_class is JSONResponse:
                if hasattr(result, "model_dump"):
                    return JSONResponse(result.model_dump())
                return JSONResponse(result)
            if isinstance(result, str):
                return response_class(result)
            if isinstance(result, bytes):
                return response_class(result)
            return response_class(result)
        if isinstance(result, (dict, list)):
            return JSONResponse(result)
        if isinstance(result, str):
            # A bare `str` return defaults to text/html — the same default
            # `make_response()` applies, so the media type is consistent
            # whichever path produced the response.
            return Response(body=result.encode(), content_type="text/html; charset=utf-8")
        if isinstance(result, bytes):
            return Response(body=result, content_type="application/octet-stream")
        # Pydantic model
        if hasattr(result, "model_dump"):
            return JSONResponse(result.model_dump())
        # Tuple response (body, status_code) or (body, status_code, headers)
        if isinstance(result, tuple):
            if len(result) == 2:
                body, code = result
                resp = self._coerce_response(body)
                resp.status_code = code
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
        self, request: Request, status_code: int, duration_ms: float
    ) -> None:
        """Deliver a `RequestMetrics` record to every instrumentation hook.

        A hook may be sync or async; one that raises is logged and skipped
        so observability code can never break the response.
        """
        from veloce.instrumentation import RequestMetrics

        metrics = RequestMetrics(
            method=request.method,
            path=request.path,
            route=request.url_rule,
            status_code=status_code,
            duration_ms=duration_ms,
        )
        for hook in self._instrumentation:
            try:
                result = hook(metrics)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                self.logger.exception("instrumentation hook raised an exception")

    # ── Server ───────────────────────────────────────────────────

    def _setup_openapi(self) -> None:
        """Register OpenAPI/Swagger routes if enabled."""
        if self._openapi_setup:
            return
        self._openapi_setup = True
        if self._openapi_url:
            from veloce.contrib.openapi import setup_openapi_routes

            # Pass the configured URLs through unchanged — `None` means
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
        host: str = "0.0.0.0",
        port: int = 8000,
        workers: int = 1,
        access_log: bool = True,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Start the built-in **development** server.

        Veloce's from-scratch HTTP server is intended for local
        development only. For production, run the app under a hardened
        ASGI server — ``uvicorn your_module:app`` — which veloce is fully
        compatible with through its ASGI ``__call__`` interface.
        ``run()`` logs a reminder of this on startup.

        ``ssl_context`` — an ``ssl.SSLContext`` — turns on HTTPS for local
        testing; it is handed straight to ``loop.create_server(ssl=...)``.
        Left ``None`` (the default) the serving path is byte-for-byte the
        same as plain HTTP. Production should still terminate TLS at
        uvicorn or a reverse proxy.
        """
        self._setup_openapi()

        # The from-scratch server is dev-grade — make the production
        # recommendation impossible to miss.
        self.logger.warning(
            "veloce's built-in server (app.run()) is for local development only — "
            "run under uvicorn (or another hardened ASGI server) in production."
        )

        # Debug tracebacks leak source and internals — binding a non-local
        # host with debug=True exposes them to the network.
        if self.debug and host not in ("127.0.0.1", "::1", "localhost"):
            self.logger.warning(
                "debug=True with a non-local bind (host=%r) exposes debug "
                "tracebacks to the network — set debug=False for any deployment "
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
            scheme = "https" if ssl_context is not None else "http"
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
        from veloce.serving.protocol import HttpProtocol

        loop = asyncio.get_running_loop()
        self._server_ref = None

        # Run startup hooks
        await self._run_lifecycle("startup")

        # `ssl=None` (the default) makes `create_server` behave exactly as
        # the plain-HTTP path; TLS cost is paid only when a context is set.
        server = await loop.create_server(
            lambda: HttpProtocol(self, loop),
            host,
            port,
            reuse_port=True,
            ssl=ssl_context,
        )
        self._server_ref = server

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
        await self._run_lifecycle("shutdown")

        # Run teardown_appcontext hooks
        for hook in self._teardown_appcontext_hooks:
            try:
                if inspect.iscoroutinefunction(hook):
                    await hook(None)
                else:
                    hook(None)
            except Exception:
                self.logger.exception("teardown_appcontext hook raised an exception")

    async def _run_lifecycle(self, event: str) -> None:
        """Run lifecycle event handlers, including lifespan context manager."""
        if event == "startup":
            # Lifespan context manager
            if self._lifespan is not None:
                self._lifespan_cm = self._lifespan(self)
                await self._lifespan_cm.__aenter__()

            for handler in self._on_startup:
                if inspect.iscoroutinefunction(handler):
                    await handler()
                else:
                    handler()

            # Dev-mode event-loop blocking watchdog — opt-in, so an app
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
                if inspect.iscoroutinefunction(handler):
                    await handler()
                else:
                    handler()

            # Exit lifespan context manager
            if self._lifespan_cm is not None:
                await self._lifespan_cm.__aexit__(None, None, None)
                self._lifespan_cm = None

    def lifespan_context(self) -> _LifespanManager:
        """Return an async context manager driving the lifespan cycle.

        `async with app.lifespan_context(): ...` runs the full startup
        sequence (lifespan CM enter + `on_startup` handlers) on entry
        and the shutdown sequence on exit — independent of any request.
        Useful for tests and for embedding the app where you want
        startup/shutdown without an ASGI server in the loop.
        """
        return _LifespanManager(self)

    # ── ASGI compatibility layer ─────────────────────────────────

    async def _emit_413(self, send: Callable, limit: int) -> None:
        """Emit a 413 response directly over ASGI.

        Used by the incremental body-size guard in `__call__`, which
        runs before a `Request` object exists.
        """
        resp = JSONResponse(
            {
                "detail": "Request body exceeds MAX_CONTENT_LENGTH",
                "status_code": 413,
                "limit": limit,
            },
            status_code=413,
        )
        body = resp.body
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", resp.content_type.encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

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
        """ASGI interface — allows running under uvicorn/hypercorn if desired.

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
        """The core ASGI application — HTTP / WebSocket / lifespan handling."""
        self._setup_openapi()

        # Mounted arbitrary ASGI apps are dispatched here with the raw
        # scope — the matched prefix is moved from `path` to `root_path`.
        if self._asgi_mounts and scope["type"] in ("http", "websocket"):
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

        if scope["type"] == "http":
            # Build request from ASGI scope. Construct headers from the raw
            # tuple list so duplicate headers (Set-Cookie, etc.) are preserved.
            from veloce.http.datastructures import Headers

            # Ingest the raw ASGI byte pairs as a list rather than a
            # generator — `CIMultiDict` consumes the list in one tight
            # loop, avoiding a generator-frame resume per header while
            # keeping duplicate-header and case-insensitive semantics.
            headers = Headers(
                [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])]
            )

            # Enforce MAX_CONTENT_LENGTH while the body is still being
            # received, so an oversized upload is rejected before the whole
            # payload is buffered into memory (RFC 9110 §15.5.14). A declared
            # Content-Length over the limit is refused up front; the running
            # total catches chunked bodies that omit it.
            max_size = self.config.get("MAX_CONTENT_LENGTH")
            if max_size is not None:
                declared = headers.get("content-length")
                if declared is not None:
                    try:
                        over = int(declared) > max_size
                    except ValueError:
                        over = False
                    if over:
                        await self._emit_413(send, max_size)
                        return

            body_parts = []
            received = 0
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

            path = scope.get("path", "/")
            query = scope.get("query_string", b"").decode("ascii")

            request = Request(
                method=scope["method"],
                path=path,
                query_string=query,
                headers=headers,
                body=b"".join(body_parts),
                scope=scope,
            )

            response = await self.handle_request(request)

            # Streaming response — emit the body as a sequence of ASGI
            # `http.response.body` chunks instead of one buffered
            # payload. No `content-length`: the ASGI server frames it.
            if response.is_streamed:
                # CRLF-validate every header value — the ASGI emit path
                # bypasses `Response.encode()`, so the splitting guard must
                # be applied here too.
                stream_headers: list[tuple[bytes, bytes]] = [
                    (
                        b"content-type",
                        _reject_header_crlf(response.content_type, "content-type").encode(),
                    ),
                ]
                for sk, sv in response.headers.items():
                    sk_lower = sk.lower()
                    if sk_lower == "content-length":
                        continue
                    if sk_lower == "set-cookie":
                        for piece in sv.split("\r\nSet-Cookie:"):
                            cookie = piece.strip()
                            _reject_header_crlf(cookie, "Set-Cookie value")
                            stream_headers.append((b"set-cookie", cookie.encode()))
                    else:
                        _reject_header_crlf(sk, "header name")
                        _reject_header_crlf(sv, f"{sk} header value")
                        stream_headers.append((sk_lower.encode(), sv.encode()))
                await send(
                    {
                        "type": "http.response.start",
                        "status": response.status_code,
                        "headers": stream_headers,
                    }
                )
                if scope["method"] != "HEAD":
                    async for chunk in getattr(response, "_stream"):  # noqa: B009
                        await send(
                            {
                                "type": "http.response.body",
                                "body": chunk.encode("utf-8") if isinstance(chunk, str) else chunk,
                                "more_body": True,
                            }
                        )
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return

            # RFC 9110 §15.3.5 (204), §15.4.5 (304), §15.3.6 (205 — must
            # contain no body either): responses with these status codes
            # MUST NOT include a payload. Strip the body before sending so
            # buggy handlers can't violate the spec.
            body_out = response.body
            if response.status_code in (204, 304, 205):
                body_out = b""

            # RFC 9110 §9.3.2: HEAD responses must not include a payload
            # body, but `Content-Length` (and other content-related
            # headers) should still reflect the size the equivalent GET
            # would have produced. Capture the real length first, then
            # blank the body — preserves HEAD's "probe for size" use case.
            head_content_length: int | None = None
            if scope["method"] == "HEAD":
                head_content_length = len(body_out)
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
            # CRLF-validate every header value — the ASGI emit path
            # bypasses `Response.encode()`, so the response-splitting
            # guard must be applied here too.
            asgi_headers: list[tuple[bytes, bytes]] = [
                (
                    b"content-type",
                    _reject_header_crlf(response.content_type, "content-type").encode(),
                ),
                (b"content-length", str(content_length).encode()),
            ]
            for k, v in response.headers.items():
                k_lower = k.lower()
                if k_lower == "set-cookie":
                    # `Response.set_cookie` joins multiple cookies into one
                    # header value with `\r\nSet-Cookie: ` literal for the
                    # raw HTTP/1.1 wire path. Split it back into per-cookie
                    # ASGI tuples regardless of how many cookies are there.
                    for piece in v.split("\r\nSet-Cookie:"):
                        cookie = piece.strip()
                        _reject_header_crlf(cookie, "Set-Cookie value")
                        asgi_headers.append((b"set-cookie", cookie.encode()))
                else:
                    _reject_header_crlf(k, "header name")
                    _reject_header_crlf(v, f"{k} header value")
                    asgi_headers.append((k_lower.encode(), v.encode()))

            await send(
                {
                    "type": "http.response.start",
                    "status": response.status_code,
                    "headers": asgi_headers,
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": body_out,
                }
            )

        elif scope["type"] == "websocket":
            # ASGI WS dispatch (W1). Match the route table for a
            # WEBSOCKET-method handler and run it with a WebSocket built
            # from the ASGI receive/send pair. Path params are coerced
            # the same way they are for HTTP.
            from veloce.websocket import WebSocket

            # Host and Origin validation for WebSocket handshakes — an HTTP
            # middleware such as TrustedHostMiddleware or
            # WebSocketOriginMiddleware never sees a `websocket` scope, so
            # apply any host allow-list and Origin allow-list directly here.
            ws_host = ""
            ws_origin = ""
            _host_seen = False
            _origin_seen = False
            for _hk, _hv in scope.get("headers", []):
                # First occurrence of each header wins — a duplicate
                # `Origin` must not be able to shadow the real one.
                if _hk == b"host" and not _host_seen:
                    ws_host = _hv.decode("latin-1").split(":", 1)[0].lower()
                    _host_seen = True
                elif _hk == b"origin" and not _origin_seen:
                    ws_origin = _hv.decode("latin-1")
                    _origin_seen = True
            for _mw in self._middlewares:
                _host_check = getattr(_mw, "is_host_allowed", None)
                if _host_check is not None and not _host_check(ws_host):
                    msg = await receive()
                    if msg["type"] == "websocket.connect":
                        await send({"type": "websocket.close", "code": 1008})
                    return
                _origin_check = getattr(_mw, "is_websocket_origin_allowed", None)
                if _origin_check is not None and not _origin_check(ws_origin):
                    msg = await receive()
                    if msg["type"] == "websocket.connect":
                        await send({"type": "websocket.close", "code": 1008})
                    return

            ws_match = self.match("WEBSOCKET", scope.get("path", "/"))
            if ws_match is None:
                # No handler — refuse the connection per ASGI WS spec.
                msg = await receive()
                if msg["type"] == "websocket.connect":
                    await send({"type": "websocket.close", "code": 1008})
                return

            ws = WebSocket.from_asgi(scope, receive, send)
            ws.path_params = ws_match.path_params
            route_info = ws_match.route_info
            # A fresh resolver per connection: a WebSocket is long-lived,
            # so its yield-dependency teardown stack must not be cleared
            # by a concurrent request resetting the shared HTTP resolver.
            ws_resolver = DependencyResolver()
            ws_resolver._overrides = self._dependency_overrides
            ws_exc: BaseException | None = None
            try:
                handler = route_info.handler
                # WebSocket DI runs through the shared HandlerPlan /
                # DependencyResolver — the same path as HTTP dispatch — so
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
                        # A WebSocket dependency failed validation —
                        # surface it as the WS-specific error (V9).
                        raise WebSocketRequestValidationError(
                            getattr(exc, "errors", []) or []
                        ) from exc
                else:
                    kwargs = {}
                await handler(**kwargs)
            except WebSocketRequestValidationError:
                # Dependency validation failure — close with 1008
                # (policy violation), not 1011, and swallow.
                if not ws._closed:
                    with contextlib.suppress(Exception):
                        await ws.close(code=1008)
            except WebSocketException as exc:
                # Application-driven close — send the requested code +
                # reason and swallow the exception (not an error).
                if not ws._closed:
                    with contextlib.suppress(Exception):
                        await ws.close(code=exc.code, reason=exc.reason or "")
            except Exception as exc:
                ws_exc = exc
                if not ws._closed:
                    with contextlib.suppress(Exception):
                        await ws.close(code=1011)  # internal error
                raise
            else:
                if not ws._closed:
                    with contextlib.suppress(Exception):
                        await ws.close()
            finally:
                # Drain any `yield`-style dependency teardowns the
                # handshake set up, exception-aware.
                await ws_resolver.run_teardowns(ws_exc)

        elif scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await self._run_lifecycle("startup")
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await self._run_lifecycle("shutdown")
                    await send({"type": "lifespan.shutdown.complete"})
                    return


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
        await self._app._run_lifecycle("startup")
        return self._app

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._app._run_lifecycle("shutdown")
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
        from veloce.helpers import _current_app_var, _RequestGlobals
        from veloce.signals import appcontext_pushed

        self._app_token = _current_app_var.set(self._app)
        # Fresh `g` store — each app_context block gets its own.
        self._g_token = _RequestGlobals._ctx_var.set({})
        appcontext_pushed.send(self._app)
        return self._app

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        from veloce.helpers import _current_app_var, _RequestGlobals
        from veloce.signals import appcontext_popped, appcontext_tearing_down

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
    — that's what `TestClient` is for. This is for unit tests that just
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
        from veloce.helpers import _current_request_var

        self._request_token = _current_request_var.set(self._request)
        return self._request

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        from veloce.helpers import _current_request_var

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

    __slots__ = ("_app",)

    def __init__(self, app: Veloce) -> None:
        self._app = app

    def _build(self) -> list[URLRule]:
        # Collect every (method, path, info) tuple, then group by
        # (path, endpoint-name) so a route registered for both GET and
        # POST shows up as a single rule.
        groups: dict[tuple[str, str], URLRule] = {}
        for method, path, info in self._app._collect_all_routes():
            key = (path, info.name)
            existing = groups.get(key)
            if existing is None:
                groups[key] = URLRule(rule=path, methods=[method], endpoint=info.name)
            else:
                existing.methods.append(method)
        return list(groups.values())

    def __iter__(self) -> Any:
        return iter(self._build())

    def __len__(self) -> int:
        return len(self._build())

    def __getitem__(self, endpoint: str) -> list[URLRule]:
        return [r for r in self._build() if r.endpoint == endpoint]

    def __repr__(self) -> str:
        rules = self._build()
        return f"<URLMap with {len(rules)} rule{'s' if len(rules) != 1 else ''}>"
