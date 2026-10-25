"""Background-task supervision — the app's spawn / supervise machinery.

Mixed into `Veloce`. These methods manage long-lived tasks the application
spawns, not request handlers, so they live off the per-request path.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from typing_extensions import Doc

from veloce.app._host import AppHost


class BackgroundTasksMixin(AppHost):
    """Spawn, name, supervise, and drain application background tasks."""

    def spawn(
        self,
        coro: Annotated[Coroutine[Any, Any, Any], Doc("Coroutine to run as a background task.")],
        *,
        name: Annotated[
            str | None, Doc("Name the task is registered under; anonymous when omitted.")
        ] = None,
    ) -> asyncio.Task[Any]:
        """Schedule a long-lived, app-scoped background task.

        A spawned task lives for the application's lifetime: it is tracked with
        a strong reference, so the loop cannot GC it mid-flight, and is
        cancelled-and-drained at shutdown within the `GRACEFUL_TASK_TIMEOUT`
        budget. Pass `name` to make it retrievable and cancellable by name; a
        duplicate name raises. Requires a running event loop.

        Usage::

            @app.on_startup
            async def _start_poller():
                app.spawn(poll_queue(), name="queue-poller")
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(
                "app.spawn() requires a running event loop; call it from an "
                "on_startup handler, the lifespan context, or a request handler."
            ) from exc
        if name is not None and name in self._spawned_named:
            raise ValueError(f"a spawned task named {name!r} already exists")
        task = loop.create_task(coro, name=name)
        if name is not None:
            self._spawned_named[name] = task
        else:
            self._spawned_anon.add(task)
        task.add_done_callback(self._spawned_task_done)
        return task

    def get_spawned_task(self, name: str) -> asyncio.Task[Any] | None:
        """Return the named spawned task, or `None` if there is no such task."""
        return self._spawned_named.get(name)

    def cancel_spawned_task(self, name: str) -> bool:
        """Cancel a named spawned task. Return whether a task was cancelled."""
        task = self._spawned_named.get(name)
        if task is None:
            return False
        task.cancel()
        return True

    def supervise(
        self,
        coro_factory: Annotated[
            Callable[[], Coroutine[Any, Any, Any]],
            Doc("Zero-argument callable returning a fresh coroutine on every restart."),
        ],
        *,
        name: Annotated[str, Doc("Name the supervised task is registered under.")],
        max_restarts: Annotated[
            int, Doc("Restarts allowed inside `restart_window` before giving up.")
        ] = 5,
        restart_window: Annotated[float, Doc("Seconds the restart budget is counted over.")] = 60.0,
        backoff: Annotated[float, Doc("Seconds to wait before the first restart.")] = 1.0,
        max_backoff: Annotated[float, Doc("Ceiling the doubling backoff is clamped to.")] = 30.0,
    ) -> asyncio.Task[Any]:
        """Run a long-lived coroutine, restarting it on failure.

        `coro_factory` returns a fresh coroutine on each call - the supervisor
        calls it to start and again to restart, so a single coroutine object is
        not accepted. A normal return is restarted too, a raise is logged and
        restarted after a bounded backoff, and `asyncio.CancelledError` is never
        suppressed.

        A count-within-window breaker bounds runaway restarts: at most
        `max_restarts` within any `restart_window` seconds, with the counter and
        the doubling `backoff` reset by any run longer than the window. The
        supervisor itself runs as an `app.spawn(...)` task, so `name` is
        required and a duplicate raises.

        Usage::

            @app.on_startup
            async def _start():
                app.supervise(lambda: poll_queue(), name="queue-poller")
        """
        if not callable(coro_factory):
            raise TypeError(
                "app.supervise() requires a zero-argument callable returning a "
                "fresh coroutine (e.g. `lambda: worker()`), not a coroutine "
                "object - a coroutine cannot be re-awaited after a restart."
            )
        # Before building the supervisor coroutine, so a rejected call leaves no
        # un-awaited coroutine behind.
        if name in self._spawned_named:
            raise ValueError(f"a spawned task named {name!r} already exists")
        return self.spawn(
            self._supervise_loop(
                coro_factory,
                name=name,
                max_restarts=max_restarts,
                restart_window=restart_window,
                backoff=backoff,
                max_backoff=max_backoff,
            ),
            name=name,
        )

    async def wait_for_background_tasks(self, timeout: float | None = 5.0) -> bool:
        """Wait for every currently-spawned background task to finish.

        `True` when they all completed, `False` when `timeout` elapsed first.
        Tasks are left running either way - this waits, it does not cancel.
        Newly spawned tasks are picked up too, so a task that spawns another is
        waited for in full.

        Usage::

            await app.wait_for_background_tasks()
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            tasks = [
                task
                for task in (*self._spawned_named.values(), *self._spawned_anon)
                if not task.done()
            ]
            if not tasks:
                return True
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return False
            await asyncio.wait(tasks, timeout=remaining)

    def _log_background_task_error(self, task: asyncio.Task[Any]) -> None:
        """Report a finished task's failure, called from `_spawned_task_done`.

        Pulling the exception off the future also silences the
        `Task exception was never retrieved` warning. Never re-raises: nothing
        awaits a spawned task, so there is no caller to raise into.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.logger.error("Background task failed", exc_info=exc)

    def _spawned_task_done(self, task: asyncio.Task[Any]) -> None:
        """Done-callback: drop the strong ref and log any non-cancel failure."""
        name = task.get_name()
        if self._spawned_named.get(name) is task:
            del self._spawned_named[name]
        self._spawned_anon.discard(task)
        self._log_background_task_error(task)

    async def _supervise_loop(
        self,
        coro_factory: Callable[[], Coroutine[Any, Any, Any]],
        *,
        name: str,
        max_restarts: int,
        restart_window: float,
        backoff: float,
        max_backoff: float,
    ) -> None:
        """Drive `coro_factory` forever, restarting on failure with a breaker.

        Re-raises `asyncio.CancelledError` immediately so shutdown cancellation
        propagates. Once the sliding window's failure count reaches
        `max_restarts` the supervisor logs and returns rather than restarting.
        """
        failures = 0
        window_start = time.monotonic()
        delay = backoff
        while True:
            started = time.monotonic()
            try:
                # A factory doing synchronous setup may raise, which is a crash
                # like any other and is restarted rather than propagated.
                awaitable = coro_factory()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - logged and restarted
                self.logger.error("Supervised task %r crashed; will restart", name, exc_info=exc)
            else:
                # A non-awaitable RETURN is a programmer error, not a crash, so
                # it fails fast rather than retrying to the breaker.
                if not inspect.isawaitable(awaitable):
                    raise TypeError(
                        f"app.supervise() factory for {name!r} must return a fresh "
                        f"awaitable on each call (e.g. `lambda: worker()`); got "
                        f"{type(awaitable).__name__}."
                    )
                try:
                    await awaitable
                except asyncio.CancelledError:
                    # Propagated, so the task drains rather than restarting.
                    raise
                except BaseException as exc:  # noqa: BLE001 - logged and restarted
                    self.logger.error(
                        "Supervised task %r crashed; will restart", name, exc_info=exc
                    )
                else:
                    # A supervised task runs for the app's lifetime, so a silent
                    # exit is logged rather than left dead.
                    self.logger.warning("Supervised task %r returned; restarting", name)
            now = time.monotonic()
            # A run longer than the window is a clean run, so only tight crash
            # loops accumulate.
            if now - started >= restart_window:
                failures = 0
                window_start = now
                delay = backoff
            else:
                # Slid forward from the first counted failure, so failures spaced
                # further apart than the window never trip the breaker.
                if now - window_start >= restart_window:
                    failures = 0
                    window_start = now
                    delay = backoff
                failures += 1
                # `max_restarts` counts RESTARTS, not failed runs; `>=` here
                # would make `max_restarts=1` retry zero times.
                if failures > max_restarts:
                    self.logger.error(
                        "Supervised task %r exceeded %d restarts within %.0fs; giving up",
                        name,
                        max_restarts,
                        restart_window,
                    )
                    return
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_backoff)

    async def _drain_spawned_tasks(self) -> None:
        """Cancel and await every spawned task within the per-task budget.

        Run twice by the shutdown lifecycle: once before the `on_shutdown`
        handlers, and again in a `finally` after they and the lifespan stack
        have unwound, so a task spawned by a teardown callback is drained too.
        One that ignores cancellation past `GRACEFUL_TASK_TIMEOUT` is abandoned.
        """
        tasks = [*self._spawned_named.values(), *self._spawned_anon]
        if not tasks:
            return
        timeout = self.config.get("GRACEFUL_TASK_TIMEOUT", 10)
        for task in tasks:
            task.cancel()
        # `wait` reports an errored or cancelled task as done rather than
        # raising, and one past the budget lands in `pending` and is left behind.
        await asyncio.wait(tasks, timeout=timeout)
        self._spawned_named.clear()
        self._spawned_anon.clear()
