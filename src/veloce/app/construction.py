"""Construction helpers — the `_init_*` steps `Veloce.__init__` runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from veloce._internal import (
    _UNRESOLVED_JSON_DUMPS,
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
)
from veloce.app._host import AppHost
from veloce.app.lifecycle import LifecycleMixin
from veloce.blueprints import Blueprint

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app.core import Veloce
    from veloce.contrib.staticfiles import StaticFiles
    from veloce.middleware import Middleware


class ConstructionMixin(AppHost):
    """The state-initialisation steps of `Veloce.__init__`."""

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
        # True from the first dispatch, so late registration raises rather than
        # racing in-flight requests.
        self._setup_locked = False
        # The in-memory `TestClient` clears it so a test can keep registering.
        self._setup_lock_enabled = True
        # In startup order, so a mid-startup failure unwinds what succeeded.
        self._lifespan_stack: contextlib.AsyncExitStack | None = None
        # Newest-first BEFORE the parent's `on_shutdown`, so a child releasing
        # work against a shared resource finds it still open.
        self._started_subapps: list[LifecycleMixin] = []
        # Strong references, so the loop cannot GC an in-flight task.
        self._spawned_named: dict[str, asyncio.Task[Any]] = {}
        self._spawned_anon: set[asyncio.Task[Any]] = set()

    def _init_middleware_state(self) -> None:
        """Set up the app logger and the ledger middleware registration writes to."""
        self.logger = logging.getLogger(self.import_name)

        self._middlewares: list[Middleware] = []
        # `(priority, sequence, instance)`; the sort is skipped until a priority
        # is set, and `sequence` orders equals.
        self._middleware_records: list[tuple[int, int, Middleware]] = []
        self._middleware_seq = 0
        self._any_priority = False
        # `RouteInfo._mw_chain_cache` keys on this, so a filtered chain is only
        # recomputed when the middleware set changes.
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
        # Handshake host / origin gate, pre-filtered to a tuple.
        self._features.append(
            FeatureSpec(
                "ws.handshake",
                PH_WS_HANDSHAKE,
                enabled=lambda: bool(self._middlewares),
                build=lambda: build_ws_handshake_checks(cast("Veloce", self)),
            )
        )
        # `process_response` reversed and fused once, so no per-response alloc.
        self._features.append(
            FeatureSpec(
                "http.middleware.response",
                PH_HTTP_POST,
                enabled=lambda: bool(self._middlewares),
                build=lambda: build_response_middleware(cast("Veloce", self)),
            )
        )
        # The `process_request` methods in forward order, on the same terms.
        self._features.append(
            FeatureSpec(
                "http.middleware.request",
                PH_HTTP_PRE,
                enabled=lambda: bool(self._middlewares),
                build=lambda: build_request_middleware(cast("Veloce", self)),
            )
        )
        # The `@app.middleware("http")` chain; `None` skips the around branch.
        self._features.append(
            FeatureSpec(
                "http.middleware.around",
                PH_HTTP_AROUND,
                enabled=lambda: bool(self._http_middleware_funcs),
                build=lambda: tuple(self._http_middleware_funcs),
            )
        )
        # `None` un-instrumented, which then never reads the perf clock.
        self._features.append(
            FeatureSpec(
                "http.instrumentation",
                PH_HTTP_FINISH,
                enabled=lambda: bool(self._instrumentation),
                build=lambda: tuple(self._instrumentation),
            )
        )
        # At the default `order`, so a higher-order wrapper sorts outside it.
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
        # Rebuilt when the pipeline generation advances, so no manual reset.
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
        # Cached MRO walks; unbounded, but an app raises a fixed set of classes.
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
        # `(sub_plan, is_coro, is_gen, is_async_gen)` per override, so a fresh
        # per-request resolver skips the build and the triple probe.
        self._override_subplans: weakref.WeakKeyDictionary[Callable[..., Any], Any] = (
            weakref.WeakKeyDictionary()
        )
        self._before_request_hooks: list[Callable[..., Any]] = []
        self._before_first_request_hooks: list[Callable[..., Any]] = []
        # Stops two concurrent first requests both seeing it False. `threading`
        # rather than `asyncio`: an `asyncio.Lock` binds to the first loop that
        # blocks on it, and one app can be served by a loop per thread.
        self._first_request_fired = False
        self._first_request_guard = threading.Lock()
        self._first_request_running = False
        self._after_request_hooks: list[Callable[..., Any]] = []
        self._teardown_request_hooks: list[Callable[..., Any]] = []
        # Dispatch walks only the bucket matching the route's `endpoint` prefix.
        self._bp_before_hooks: dict[str, list[Callable[..., Any]]] = {}
        self._bp_after_hooks: dict[str, list[Callable[..., Any]]] = {}
        self._bp_teardown_hooks: dict[str, list[Callable[..., Any]]] = {}
        # Out of the app-global tables, so never a sibling blueprint's routes.
        self._bp_exception_handlers: dict[str, dict[type[Any], Callable[..., Any]]] = {}
        self._bp_status_handlers: dict[str, dict[int, Callable[..., Any]]] = {}
        self._teardown_appcontext_hooks: list[Callable[..., Any]] = []
        self._context_processors: list[Callable[..., Any]] = []
        # `(prefix, prefix + "/", sub_app)`, the boundary precomputed.
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
        # A preprocessor runs after route match; url_defaults inside `url_for`.
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
        # `None` is the stock provider, which the direct backend path matches.
        self._handler_json_dumps: Any = _UNRESOLVED_JSON_DUMPS
        # Built on first `app.aborter` access, so a subclass can replace it first.
        self._aborter = None
        # Relative to `package_root`; mounting `StaticFiles` is opt-in.
        self.static_folder: str = "static"
        self.static_url_path: str = "/static"
