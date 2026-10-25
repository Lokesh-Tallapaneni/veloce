"""Request and application lifecycle — hooks and lifespan mixed into Veloce.

Holds the request hooks and their teardown runners, the lifespan-event
registration surface, and the lifespan engine (`_run_lifecycle` +
`lifespan_context`) that fans startup out to mounted sub-apps and unwinds them
in reverse on shutdown. A mixin on `Veloce`, none of it on the hot path.
"""

from __future__ import annotations

import asyncio
import contextlib
import warnings
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, cast

from veloce._handler_plan import K_DEPENDS
from veloce._internal import T_route, _BaseExceptionGroup, _is_async_callable, offload
from veloce._protocol_constants import (
    LIFECYCLE_SHUTDOWN,
    LIFECYCLE_STARTUP,
)
from veloce._warnings import VeloceDeprecationWarning
from veloce.app._host import AppHost
from veloce.audit import AuditFailed
from veloce.audit import run as audit_run

if TYPE_CHECKING:  # pragma: no cover
    from types import CodeType, FrameType

    from veloce._handler_plan import HandlerPlan
    from veloce.app.contexts import _LifespanManager
    from veloce.app.core import Veloce


def _collect_chained(exc: BaseException) -> list[BaseException]:
    """Flatten an exception and its `__context__` chain into a list.

    `AsyncExitStack.aclose()` chains each teardown failure onto the previous and
    re-raises the last, so walking the chain recovers all of them. The result is
    in run order, with any interior `BaseExceptionGroup` expanded.
    """
    out: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _BaseExceptionGroup is not None and isinstance(current, _BaseExceptionGroup):
            # Reversed, because the whole list is; a group's members are siblings.
            out.extend(reversed(current.exceptions))  # type: ignore[attr-defined]
        else:
            out.append(current)
        current = current.__context__
    # Reverse so the first teardown that failed leads, matching the run order.
    out.reverse()
    return out


def _raise_unwind_errors(errors: list[BaseException]) -> None:
    """Re-raise lifespan-unwind failures, grouping them when possible.

    A single failure is re-raised as-is so its traceback is preserved
    verbatim. Several failures are combined into a `BaseExceptionGroup`
    (Python 3.11+) so none is masked; on 3.10, where groups are unavailable,
    the first failure is raised with the rest attached as notes.
    """
    if not errors:
        return
    if len(errors) == 1:
        raise errors[0]
    if _BaseExceptionGroup is not None:
        raise _BaseExceptionGroup("lifespan shutdown failed", errors)
    first = errors[0]
    for extra in errors[1:]:
        _attach_note(first, f"+ also raised during lifespan unwind: {extra!r}")
    raise first


def _attach_note(error: BaseException, note: str) -> None:
    """Record `note` on `error`, on interpreters with and without `add_note`.

    `BaseException.add_note` is PEP 678 and arrived in 3.11, the same release as
    `BaseExceptionGroup`, so the 3.10 path that needs this has neither. Writing
    `__notes__` directly is the same record by the same name; 3.10 renders it in
    no traceback, but the failures stay attached rather than being discarded.
    """
    add_note = getattr(error, "add_note", None)
    if add_note is not None:
        add_note(note)
        return
    notes = getattr(error, "__notes__", None)
    if not isinstance(notes, list):
        notes = []
        with contextlib.suppress(AttributeError):
            error.__notes__ = notes  # type: ignore[attr-defined]
    notes.append(note)


def _build_watchdog_attributor(app: Veloce) -> Callable[[FrameType], str | None]:
    """Build a frame-to-route resolver for the event-loop watchdog.

    Maps a blocked loop frame to a `METHOD /path` label, or `METHOD /path -> dep`
    for a stall inside a dependency. The table is indexed lazily on the first
    stall, so wiring it on costs nothing until something blocks the loop.

    Keyed by code object: a callable registered on several paths is attributed to
    the first route indexed, and one without `__code__` (a class, a
    `functools.partial`) is left unattributed rather than mislabelled.
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

    # Runs in the watchdog thread on a stall, never on the loop.
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


def _unknown_event_error(event: str) -> ValueError:
    """Build the refusal both lifecycle-event entry points raise for an unknown name."""
    return ValueError(
        f"event must be {LIFECYCLE_STARTUP!r} or {LIFECYCLE_SHUTDOWN!r}, got {event!r}"
    )


class LifecycleMixin(AppHost):
    """Request hooks and application lifespan, mixed into `Veloce`."""

    # ── Before/After request hooks ────────────────────────

    def before_request(self, func: T_route) -> T_route:
        """Register a function to run before each request."""
        self._assert_mutable()
        self._before_request_hooks.append(func)
        self._gen += 1
        return func

    def before_first_request(self, func: T_route) -> T_route:
        """Register a function to run exactly once on the first request.

        A legacy hook style; lifespan startup handlers are preferred. Hooks fire
        serially in registration order, and single-fire is guarded with an
        `asyncio.Lock` so concurrent first requests cannot double-run them.
        """
        self._assert_mutable()
        self._before_first_request_hooks.append(func)
        return func

    def after_request(self, func: T_route) -> T_route:
        """Register a function to run after each request."""
        self._assert_mutable()
        self._after_request_hooks.append(func)
        self._gen += 1
        return func

    def teardown_request(self, func: T_route) -> T_route:
        """Register a function to run after request teardown.

        Called with an optional exception argument, even if an exception
        occurred.
        """
        self._assert_mutable()
        self._teardown_request_hooks.append(func)
        self._gen += 1
        return func

    def teardown_appcontext(self, func: T_route) -> T_route:
        """Register a function to run on app-context teardown."""
        self._assert_mutable()
        self._teardown_appcontext_hooks.append(func)
        self._gen += 1
        return func

    def _select_teardown_request_hooks(self, bp_name: str | None) -> list[Callable[..., Any]]:
        """Return the `teardown_request` hooks to run, app-level first.

        A matched blueprint's hooks are appended after the app-level ones. Shared
        by the HTTP dispatch `finally` and the MCP tool-call path.
        """
        if not (self._teardown_request_hooks or self._bp_teardown_hooks):
            return []
        hooks = list(self._teardown_request_hooks)
        if self._bp_teardown_hooks and bp_name is not None and bp_name in self._bp_teardown_hooks:
            hooks.extend(self._bp_teardown_hooks[bp_name])
        return hooks

    async def _run_request_teardown(self, exc: BaseException | None, bp_name: str | None) -> None:
        """Run `teardown_request` + `teardown_appcontext` for one request.

        Hooks always run - even on an exception - and receive `exc`, the failing
        exception or `None`. Shared by the HTTP dispatch `finally` and the MCP
        tool-call path, so a route exposed as a tool gets the same cleanup.
        """
        td_hooks = self._select_teardown_request_hooks(bp_name)
        if td_hooks:
            await self._run_teardown_hooks(td_hooks, exc, "teardown_request")

        # The app context pops per request: veloce has no separate app context.
        if self._teardown_appcontext_hooks:
            await self._run_teardown_hooks(
                self._teardown_appcontext_hooks, exc, "teardown_appcontext"
            )

    async def _run_teardown_hooks(
        self, hooks: list[Callable[..., Any]], exc: BaseException | None, label: str
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

    def on_event(self, event: str) -> Callable[[T_route], T_route]:
        """Register startup/shutdown event handlers.

        Deprecated: use `@app.on_startup` / `@app.on_shutdown` instead.
        Scheduled for removal in v1.0.0.
        """
        if event not in (LIFECYCLE_STARTUP, LIFECYCLE_SHUTDOWN):
            raise _unknown_event_error(event)
        warnings.warn(
            "Veloce.on_event() is deprecated and will be removed in v1.0.0; "
            "use @app.on_startup / @app.on_shutdown instead.",
            VeloceDeprecationWarning,
            stacklevel=2,
        )

        def decorator(func: T_route) -> T_route:
            self._register_lifecycle_event(event, func)
            return func

        return decorator

    def add_lifespan(self, factory: Callable[..., Any]) -> Callable[..., Any]:
        """Register an additional lifespan context manager.

        `lifespan=` is a single slot owned by the application, so a plugin or
        blueprint uses this to own a resource with paired setup and teardown
        without splitting the pair across `on_startup` / `on_shutdown`.

        `factory` is called with the app and must return an async context
        manager. Every registered lifespan is entered on the same exit stack as
        `lifespan=`, so teardown runs in reverse registration order and teardown
        errors are aggregated rather than masking one another. The yielded value
        is not consumed - a plugin holds its own handle.

        Usage::

            class BrokerPlugin:
                name = "broker"

                def install(self, app):
                    app.add_lifespan(self.lifespan)

                @contextlib.asynccontextmanager
                async def lifespan(self, app):
                    broker = await connect()
                    try:
                        yield {"broker": broker}
                    finally:
                        await broker.close()
        """
        self._extra_lifespans.append(factory)
        return factory

    def on_startup(self, func: T_route) -> T_route:
        """Register a startup event handler."""
        self._on_startup.append(func)
        return func

    def on_shutdown(self, func: T_route) -> T_route:
        """Register a shutdown event handler."""
        self._on_shutdown.append(func)
        return func

    def add_event_handler(self, event: str, func: Callable[..., Any]) -> None:
        """Imperative event-handler registration - ASGI shape.

        Deprecated: call `app.on_startup(fn)` / `app.on_shutdown(fn)`
        directly instead. Scheduled for removal in v1.0.0.
        """
        warnings.warn(
            "Veloce.add_event_handler() is deprecated and will be removed "
            "in v1.0.0; use app.on_startup(fn) / app.on_shutdown(fn) instead.",
            VeloceDeprecationWarning,
            stacklevel=2,
        )
        self._register_lifecycle_event(event, func)

    # Aliases of `on_startup` / `on_shutdown`, so either pair reads naturally.
    def before_serving(self, func: T_route) -> T_route:
        """Register a coroutine to run once at app startup. Alias of `on_startup`."""
        return self.on_startup(func)

    def after_serving(self, func: T_route) -> T_route:
        """Register a coroutine to run once at app shutdown. Alias of `on_shutdown`."""
        return self.on_shutdown(func)

    def _register_lifecycle_event(self, event: str, func: Callable[..., Any]) -> None:
        """Validate an event name and append the handler to its bucket.

        Shared by `on_event` and `add_event_handler` so the two register
        identically; raises the same `ValueError` for an unknown event name.
        """
        if event == LIFECYCLE_STARTUP:
            self._on_startup.append(func)
        elif event == LIFECYCLE_SHUTDOWN:
            self._on_shutdown.append(func)
        else:
            raise _unknown_event_error(event)

    # ── Lifespan engine ───────────────────────────────────

    def lifespan_context(self) -> _LifespanManager:
        """Return an async context manager driving the lifespan cycle.

        `async with app.lifespan_context(): ...` runs the full startup sequence
        on entry and the shutdown sequence on exit, independent of any request -
        for tests, and for embedding the app without an ASGI server.
        """
        # Lazy to keep the context machinery off `import veloce`, not for a cycle.
        from veloce.app.contexts import _LifespanManager

        return _LifespanManager(cast("Veloce", self))

    async def _run_handler(self, handler: Callable[..., Any]) -> None:
        """Invoke a lifecycle handler, offloading sync ones to a thread.

        A plain `def` handler runs in the default executor under a copied
        context, so it cannot block the event loop.
        """
        if _is_async_callable(handler):
            await handler()
        else:
            await offload(handler)

    async def _run_lifecycle(self, event: str) -> None:
        """Run the startup or shutdown sequence.

        The string is the transport's own vocabulary (`lifespan.startup` /
        `lifespan.shutdown`), translated here rather than at ten call sites.
        """
        if event == LIFECYCLE_STARTUP:
            await self._run_startup()
        else:
            await self._run_shutdown()

    async def _run_startup(self) -> None:
        """Acquire the lifespan CM, the watchdog and every `on_startup` handler.

        All of it onto a single `AsyncExitStack` stored on the app, so a handler
        raising mid-way unwinds exactly what was acquired before the error
        propagates and a partially started app leaves no orphaned resources.
        """
        stack = contextlib.AsyncExitStack()
        try:
            # Entered first so it exits last, outliving the handlers that use it.
            if self._lifespan is not None:
                self._lifespan_cm = self._lifespan(self)
                await stack.enter_async_context(self._lifespan_cm)

            # After the app's own, so they exit first: a plugin may depend on it.
            for factory in self._extra_lifespans:
                await stack.enter_async_context(factory(self))

            for handler in self._on_startup:
                await self._run_handler(handler)

            # A mounted child gets no ASGI lifespan of its own, so startup fans
            # out here; a non-Veloce mount owns its lifecycle and is skipped.
            self._started_subapps = []
            seen_subs: set[int] = set()
            for _prefix, _prefix_slash, sub in self._mounted_apps:
                if isinstance(sub, LifecycleMixin) and id(sub) not in seen_subs:
                    seen_subs.add(id(sub))
                    await sub._run_lifecycle(LIFECYCLE_STARTUP)
                    self._started_subapps.append(sub)

            # Opt-in: truthy, or a mapping of kwargs. On the stack, so it stops.
            watchdog_config = self.config.get("EVENT_LOOP_WATCHDOG")
            if watchdog_config and self._watchdog is None:
                from veloce.watchdog import EventLoopWatchdog

                watchdog_kwargs = (
                    dict(watchdog_config) if isinstance(watchdog_config, Mapping) else {}
                )
                self._watchdog = EventLoopWatchdog(
                    asyncio.get_running_loop(),
                    attributor=_build_watchdog_attributor(cast("Veloce", self)),
                    **watchdog_kwargs,
                )
                self._watchdog.start()
                stack.push_async_callback(self._stop_watchdog)

            # The first moment the route table is final, so a route-reading check
            # can run. An `error` fails the boot; the rest is debug-only.
            _findings = audit_run(cast("Veloce", self), routes_final=True)
            _fatal = [f for f in _findings if f.severity == "error"]
            if _fatal:
                raise AuditFailed(_fatal)
            if self.debug:
                for _audit_finding in _findings:
                    self.logger.warning("audit: %s", _audit_finding)

        except BaseException:
            # Children, then the parent's stack; unwind errors must not mask this.
            with contextlib.suppress(Exception):
                await self._shutdown_subapps()
            with contextlib.suppress(Exception):
                await stack.aclose()
            self._lifespan_cm = None
            raise
        self._lifespan_stack = stack

    async def _run_shutdown(self) -> None:
        """Drain spawned tasks, run every `on_shutdown` handler, close the stack.

        One handler raising never skips the rest: every failure is collected and
        re-raised grouped, so no teardown error is masked by a later one.
        """
        shutdown_stack = self._lifespan_stack
        self._lifespan_stack = None
        errors: list[BaseException] = []
        try:
            # Parent tasks first, so a parent loop cannot touch closed child state.
            await self._drain_spawned_tasks()
            # Children next, so a resource a parent handler closes is still there.
            errors.extend(await self._shutdown_subapps())
            # Newest first, collecting failures so one raising does not abort the rest.
            for handler in reversed(self._on_shutdown):
                try:
                    await self._run_handler(handler)
                except BaseException as exc:  # noqa: BLE001 - aggregated below
                    errors.append(exc)
            # No startup ran, so there is no stack; stop the watchdog directly.
            self._lifespan_cm = None
            if shutdown_stack is not None:
                try:
                    await shutdown_stack.aclose()
                except BaseException as exc:  # noqa: BLE001 - aggregated below
                    errors.extend(_collect_chained(exc))
            else:
                await self._stop_watchdog()
        finally:
            # Again last, in a `finally`, so a task a teardown spawned drains too.
            await self._drain_spawned_tasks()
        _raise_unwind_errors(errors)

    async def _shutdown_subapps(self) -> list[BaseException]:
        """Shut down started mounted sub-apps newest-first; return any errors.

        Every child is torn down even if one raises, and the started list is
        cleared so a repeat or standalone shutdown does not re-run them.
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
