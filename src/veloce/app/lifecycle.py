"""Request and application lifecycle — hooks and lifespan mixed into Veloce.

Holds the Flask-style request hooks (`before_request`, `after_request`,
`teardown_request`, `teardown_appcontext`) and their teardown runners, the
lifespan-event registration surface (`on_startup` / `on_shutdown` and the
deprecated `on_event` / `add_event_handler` aliases), and the lifespan engine
(`_run_lifecycle` + `lifespan_context`) that enters the lifespan context
manager, fans startup out to mounted sub-apps, and unwinds them in reverse on
shutdown. A mixin on `Veloce`; none of this is on the per-request hot path. Kept
out of `app.core` so the application object's lifecycle surface is one file.
"""

from __future__ import annotations

import asyncio
import contextlib
import warnings
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from veloce._handler_plan import K_DEPENDS
from veloce._internal import _BaseExceptionGroup, _is_async_callable, offload
from veloce._protocol_constants import (
    LIFECYCLE_SHUTDOWN,
    LIFECYCLE_STARTUP,
)

if TYPE_CHECKING:  # pragma: no cover
    from types import CodeType, FrameType

    from veloce._handler_plan import HandlerPlan
    from veloce.app.contexts import _LifespanManager
    from veloce.app.core import Veloce


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


def _build_watchdog_attributor(app: Veloce) -> Callable[[FrameType], str | None]:
    """Build a frame-to-route resolver for the event-loop watchdog.

    Returns a callable that maps a blocked loop frame to a `METHOD /path` (and,
    for a stall inside a dependency, `METHOD /path -> dep`) label. The route
    table is indexed lazily on the first stall and reused thereafter, so wiring
    it on costs nothing until something actually blocks the loop.

    The table is keyed by code object, so a callable shared across routes (the
    same handler or dependency registered on several paths) is attributed to the
    first route indexed; the logged stack still pinpoints the real call. A class
    or `functools.partial` dependency has no `__code__` and is left unattributed,
    degrading to the bare warning rather than a wrong label.
    """
    table: dict[CodeType, str] = {}
    built = False

    def _code_of(fn: object) -> CodeType | None:
        return getattr(fn, "__code__", None)

    def _index_plan(plan: HandlerPlan, label: str) -> None:
        for slot in plan.slots:
            if slot.kind != K_DEPENDS:
                continue
            dep = slot.dep_callable
            dep_label = f"{label} -> {getattr(dep, '__name__', None) or slot.name}"
            code = _code_of(dep)
            if code is not None:
                table.setdefault(code, dep_label)
            if slot.sub_plan is not None:
                _index_plan(slot.sub_plan, dep_label)

    def _build() -> None:
        for method, path, info in app._collect_all_routes(include_hidden=True):
            label = f"{method} {path}"
            code = _code_of(info.handler)
            if code is not None:
                table.setdefault(code, label)
            if info.handler_plan is not None:
                _index_plan(info.handler_plan, label)

    # Walk the blocked frame outward to its caller chain and return the
    # innermost handler/dependency it is running inside. Runs in the watchdog
    # thread on a stall, never on the loop.
    def attributor(frame: FrameType) -> str | None:
        nonlocal built
        if not built:
            _build()
            built = True
        f: FrameType | None = frame
        while f is not None:
            label = table.get(f.f_code)
            if label is not None:
                return label
            f = f.f_back
        return None

    return attributor


class LifecycleMixin:
    """Request hooks and application lifespan, mixed into `Veloce`."""

    if TYPE_CHECKING:  # pragma: no cover
        # Attributes / methods the host application (Veloce) provides.
        config: Any
        logger: Any
        _assert_mutable: Callable[..., Any]
        _gen: int
        _before_request_hooks: Any
        _after_request_hooks: Any
        _before_first_request_hooks: Any
        _teardown_request_hooks: Any
        _teardown_appcontext_hooks: Any
        _bp_teardown_hooks: Any
        _on_startup: Any
        _on_shutdown: Any
        _lifespan: Any
        _lifespan_cm: Any
        _lifespan_stack: Any
        _started_subapps: Any
        _mounted_apps: Any
        _watchdog: Any
        _drain_spawned_tasks: Callable[..., Any]

    # ── Before/After request hooks ────────────────────────

    def before_request(self, func: Callable) -> Callable:
        """Register a function to run before each request."""
        self._assert_mutable()
        self._before_request_hooks.append(func)
        self._gen += 1
        return func

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

    def _select_teardown_request_hooks(self, bp_name: str | None) -> list[Callable]:
        """Return the `teardown_request` hooks to run, app-level first.

        A matched blueprint's hooks are appended after the app-level ones (gated
        by the request's blueprint name). Returns an empty list when nothing is
        registered, so the caller runs no teardown. Shared by the HTTP dispatch
        `finally` and the MCP tool-call path so the two select identically.
        """
        if not (self._teardown_request_hooks or self._bp_teardown_hooks):
            return []
        hooks = list(self._teardown_request_hooks)
        if self._bp_teardown_hooks and bp_name is not None and bp_name in self._bp_teardown_hooks:
            hooks.extend(self._bp_teardown_hooks[bp_name])
        return hooks

    async def _run_request_teardown(self, exc: BaseException | None, bp_name: str | None) -> None:
        """Run `teardown_request` + `teardown_appcontext` for one request.

        Selects the matched blueprint's `teardown_request` bucket (app-level
        hooks first, then the blueprint's) and then fires the app-level
        `teardown_appcontext` hooks. Hooks always run - even on an exception -
        and receive `exc` (the failing exception or `None`). Shared by the HTTP
        dispatch `finally` and the MCP tool-call path so a route exposed as an
        MCP tool gets the same cleanup an HTTP request gets.
        """
        td_hooks = self._select_teardown_request_hooks(bp_name)
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

    # ── Lifecycle events ──────────────────────────────────

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

    # ── Lifespan engine ───────────────────────────────────

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
                # non-Veloce ASGI mount owns its own lifecycle and is skipped -
                # `LifecycleMixin` is the marker that identifies a Veloce sub-app.
                # The same child instance mounted under multiple prefixes is
                # started and shut down only once (deduped by identity).
                self._started_subapps = []
                _seen_subs: set[int] = set()
                for _prefix, _prefix_slash, _sub in self._mounted_apps:
                    if isinstance(_sub, LifecycleMixin) and id(_sub) not in _seen_subs:
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
                    self._watchdog = EventLoopWatchdog(
                        asyncio.get_running_loop(),
                        attributor=_build_watchdog_attributor(cast("Veloce", self)),
                        **_wd_kwargs,
                    )
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

        return _LifespanManager(cast("Veloce", self))
