"""The contract every `app/` mixin relies on the host `Veloce` providing.

`Veloce` is assembled from sixteen focused mixins, each of which reads state and
calls methods that a *sibling* provides. Every mixin used to restate the subset
it touched inside its own class-level `TYPE_CHECKING` block: 158 stubs across
thirteen modules for 102 distinct names, 56 of them redundant repeats. `config`
alone was written out seven times.

Restating a contract is how it drifts, and it had. Ten names carried more than
one annotation: `debug` was `bool` in `dispatch.py`, `errors.py` and
`serving.py` but `Any` in `lifecycle.py`; `_mw_version` was `int` in
`middleware.py` and `Any` in `dispatch.py`; the hook lists were concrete in
`introspection.py` and `Any` everywhere else. Whichever module a reader opened
decided what the type appeared to be, and the loosest copy silently won wherever
it was used.

Worse, the stubs were almost all `Any` or `Callable[..., Any]`, so mypy checked
nothing that crossed a mixin boundary. `self.match(...)`, `self.log_exception(...)`
and `self._find_scoped_exception_handler(...)` accepted any argument list on the
hot path.

The names here carry the type of their real definition rather than `Any`, so the
cross-mixin calls are checked. Everything is under `TYPE_CHECKING`: at runtime
this is an empty class, contributing one entry to the MRO and no behaviour. It
declares no `__slots__` because `Veloce` is deliberately unslotted - see the note
in `app/__init__.py`.

A mixin should carry no host stubs of its own. If one needs a name that is not
here, add it here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    import asyncio
    import contextlib
    import logging
    import weakref
    from collections.abc import Callable, Coroutine

    from veloce._pipeline import CompiledPipeline
    from veloce.app.contexts import _LifespanManager
    from veloce.app.lifecycle import LifecycleMixin
    from veloce.app.mcp import (
        MCPCompleterRegistration,
        MCPPromptRegistration,
        MCPToolRegistration,
    )
    from veloce.config import Config
    from veloce.contrib.staticfiles import StaticFiles
    from veloce.http.request import Request
    from veloce.http.response import Response
    from veloce.middleware import Middleware
    from veloce.routing.router import RouteInfo, RouteMatch


class AppHost:
    """What a mixin may rely on the host application providing.

    Mixed into every `app/` mixin so a sibling's state and methods resolve with
    their real types. Empty at runtime.
    """

    if TYPE_CHECKING:  # pragma: no cover
        # ── Public application state ──
        config: Config
        extensions: dict[str, Any]
        logger: logging.Logger
        version: str
        openapi_schema: dict[str, Any] | None
        redirect_slashes: bool
        url_build_error_handlers: list[Callable[..., Any]]

        # ── Request-phase hooks ──
        _before_request_hooks: list[Callable[..., Any]]
        _after_request_hooks: list[Callable[..., Any]]
        _teardown_request_hooks: list[Callable[..., Any]]
        _before_first_request_hooks: list[Callable[..., Any]]
        _teardown_appcontext_hooks: list[Callable[..., Any]]
        _url_value_preprocessors: list[Callable[..., Any]]
        _url_default_funcs: list[Callable[..., Any]]
        _context_processors: list[Callable[..., Any]]
        _shell_context_processors: list[Callable[..., Any]]

        # ── The same hooks, bucketed per blueprint ──
        _bp_before_hooks: dict[str, list[Callable[..., Any]]]
        _bp_after_hooks: dict[str, list[Callable[..., Any]]]
        _bp_teardown_hooks: dict[str, list[Callable[..., Any]]]
        _bp_url_value_preprocessors: dict[str, list[Callable[..., Any]]]
        _bp_url_default_funcs: dict[str, list[Callable[..., Any]]]
        _bp_exception_handlers: dict[str, dict[type, Callable[..., Any]]]
        _bp_status_handlers: dict[str, dict[int, Callable[..., Any]]]
        _blueprints_map: dict[str, Any]

        # ── Exception handling ──
        _exception_handlers: dict[type, Callable[..., Any]]
        _status_handlers: dict[int, Callable[..., Any]]
        _exc_handler_cache: dict[type, Callable[..., Any] | None]

        # ── Middleware and the compiled pipeline ──
        _middlewares: list[Middleware]
        _middleware_records: list[tuple[int, int, Middleware]]
        _middleware_seq: int
        _mw_version: int
        _http_middleware_funcs: list[Callable[..., Any]]
        _asgi_middleware: list[tuple[Any, dict[str, Any]]]
        _asgi_stack: Callable[..., Any] | None
        _asgi_stack_gen: int
        _pipeline: CompiledPipeline | None
        _gen: int

        # ── Mounts and static files ──
        _asgi_mounts: list[tuple[str, str, Any]]
        _mounted_apps: list[tuple[str, str, Any]]
        _static_handlers: list[StaticFiles]

        # ── Lifecycle ──
        _on_startup: list[Callable[..., Any]]
        _on_shutdown: list[Callable[..., Any]]
        _lifespan: Any
        _lifespan_cm: Any
        _lifespan_stack: contextlib.AsyncExitStack | None
        _extra_lifespans: list[Any]
        _started_subapps: list[LifecycleMixin]
        _first_request_fired: bool
        _first_request_lock: asyncio.Lock | None
        _watchdog: Any

        # ── Background tasks ──
        _spawned_anon: set[asyncio.Task[Any]]
        _spawned_named: dict[str, asyncio.Task[Any]]

        # ── Dependency injection ──
        _dependency_overrides: dict[Callable[..., Any], Callable[..., Any]]
        _override_subplans: weakref.WeakKeyDictionary[Callable[..., Any], Any]
        _handler_json_dumps: Any

        # ── OpenAPI and the docs routes ──
        _openapi_setup: bool
        _openapi_url: str | None
        _docs_url: str | None
        _redoc_url: str | None

        # ── Templating ──
        _template_filters: list[tuple[str, Callable[..., Any]]]
        _template_globals: list[tuple[str, Callable[..., Any]]]
        _template_tests: list[tuple[str, Callable[..., Any]]]

        # ── MCP registration ──
        _mcp_tools: list[MCPToolRegistration]
        _mcp_prompts: list[MCPPromptRegistration]
        _mcp_completers: list[MCPCompleterRegistration]
        _mcp_before_call: list[Callable[..., Any]]
        _mcp_after_call: list[Callable[..., Any]]
        _mcp_mounts: list[tuple[str, Any]]
        _mcp_prebuilt_tools: list[Any]

        # ── Observability and setup locking ──
        _instrumentation: list[Callable[..., Any]]
        _instrumentation_excludes: dict[Callable[..., Any], frozenset[str]]
        _setup_locked: bool
        _setup_lock_enabled: bool

        # ── Routing state owned by `Router` ──
        _any_priority: bool
        _cached_view_functions: dict[str, Callable[..., Any]] | None

        @property
        def debug(self) -> bool: ...

        # ── Provided by `Router` ──
        def match(self, method: str, path: str) -> RouteMatch | None: ...

        def get_allowed_methods(self, path: str) -> list[str]: ...

        def _collect_all_routes(
            self, include_hidden: bool = False
        ) -> list[tuple[str, str, RouteInfo]]: ...

        def _finalize_plans(
            self, route_info: RouteInfo, *, is_ws: bool, reuse_handler_plan: Any = None
        ) -> None: ...

        # ── Provided by sibling mixins ──
        async def handle_request(
            self, request: Request, cp: CompiledPipeline | None = None, match: Any = None
        ) -> Response: ...

        def _coerce_response(self, result: Any, response_class: Any = None) -> Response: ...

        async def _call_exc_handler(
            self, handler: Callable[..., Any], request: Request, exc: BaseException
        ) -> Any: ...

        async def _body_too_large_response(
            self, request: Request, cp: CompiledPipeline | None, max_size: int | None
        ) -> Response: ...

        def _find_scoped_exception_handler(
            self, exc_type: type, request: Request | None
        ) -> Callable[..., Any] | None: ...

        def _find_scoped_status_handler(
            self, code: int, request: Request | None
        ) -> Callable[..., Any] | None: ...

        def log_exception(self, exc: BaseException, request: Request | None = None) -> None: ...

        def make_default_options_response(
            self, path: str, allowed_methods: list[str] | None = None
        ) -> Response: ...

        def _should_propagate_exceptions(self) -> bool: ...

        async def _run_lifecycle(self, event: str) -> None: ...

        async def _run_teardown_hooks(
            self, hooks: list[Callable[..., Any]], exc: BaseException | None, label: str
        ) -> None: ...

        def _select_teardown_request_hooks(
            self, bp_name: str | None
        ) -> list[Callable[..., Any]]: ...

        def lifespan_context(self) -> _LifespanManager: ...

        def spawn(
            self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
        ) -> asyncio.Task[Any]: ...

        async def _drain_spawned_tasks(self) -> None: ...

        def _match_asgi_mount(self, path: str) -> tuple[str, Any] | None: ...

        def _path_under_mount(self, path: str) -> bool: ...

        def _setup_openapi(self) -> None: ...

        def _ensure_pipeline(self) -> CompiledPipeline: ...

        def _register_feature_state(self, target: list[Any], value: Any) -> None: ...

        def _resolve_handler_json_dumps(self) -> Any: ...

        def _assert_mutable(self) -> None: ...
