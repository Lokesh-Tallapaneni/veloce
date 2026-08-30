"""Background-task supervision — the app's spawn / supervise machinery.

Mixed into `Veloce`. These methods manage long-lived tasks the application
spawns (not request handlers), operating on the app's `_spawned_named` /
`_spawned_anon` registries and its logger, so they live off the per-request
path.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable, Coroutine
from typing import Any

from veloce.app._host import AppHost


class BackgroundTasksMixin(AppHost):
    """Spawn, name, supervise, and drain application background tasks."""

    def _log_background_task_error(self, task: asyncio.Task) -> None:
        """Report a finished task's failure, called from `_spawned_task_done`.

        Pulls the exception off the future (silencing
        `Task exception was never retrieved` warnings) and logs it via
        `self.logger` so failures are observable instead of silently dropped.
        Never re-raises: nothing awaits a spawned task, so there is no caller to
        raise into.
        """
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.logger.error("Background task failed", exc_info=exc)

    # ── App-scoped background tasks ───────────────────────

    def spawn(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        """Schedule a long-lived, app-scoped background task.

        Unlike per-request background tasks, a spawned task lives for the
        application's lifetime: it is tracked with a strong reference (so the
        loop cannot GC it mid-flight) and is cancelled-and-drained during
        shutdown, honouring the `GRACEFUL_TASK_TIMEOUT` config budget per
        task. Pass `name` to make the task retrievable and cancellable by
        name via `get_spawned_task` / `cancel_spawned_task`; a duplicate name
        raises. Failures are logged through the same path as request-scoped
        background tasks, so app and request work surface uniformly.

        Must be called with a running event loop (e.g. from within an
        `on_startup` handler, the lifespan CM, or a request); calling it
        before the loop exists raises `RuntimeError`.

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
        coro_factory: Callable[[], Coroutine[Any, Any, Any]],
        *,
        name: str,
        max_restarts: int = 5,
        restart_window: float = 60.0,
        backoff: float = 1.0,
        max_backoff: float = 30.0,
    ) -> asyncio.Task[Any]:
        """Run a long-lived coroutine, restarting it on failure.

        `coro_factory` is a zero-argument callable that returns a fresh
        coroutine each time it is invoked - the supervisor calls it to start
        the task and again to restart after a crash, so a single coroutine
        object (which cannot be re-awaited) is not accepted. The supervised
        coroutine is expected to run for the application's lifetime; if it
        returns normally the supervisor restarts it, and if it raises the
        failure is logged and the coroutine is restarted after a bounded
        backoff delay. `asyncio.CancelledError` is never suppressed, so the
        task stops cleanly when cancelled at shutdown.

        A count-within-window circuit breaker bounds runaway restarts: at most
        `max_restarts` restarts are allowed within any `restart_window` seconds.
        The restart counter resets whenever the coroutine runs for longer than
        the window without failing (a clean run), so steady-state restarts far
        apart never trip the breaker; a tight crash loop does. When the breaker
        trips the supervisor logs the give-up and stops restarting. `backoff`
        is the initial delay between restarts and doubles up to `max_backoff`
        on consecutive failures, resetting to `backoff` after a clean run.

        The supervisor itself runs as an `app.spawn(...)` task, so it is tracked
        with a strong reference and cancelled-and-drained on shutdown like any
        other spawned task. `name` is required (the supervisor task is named so
        it is retrievable / cancellable via `get_spawned_task` /
        `cancel_spawned_task`); a duplicate name raises. Must be called with a
        running event loop.

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
        # Reject a duplicate name before building the supervisor coroutine so a
        # rejected call leaves no un-awaited coroutine behind (spawn would also
        # reject, but only after the coroutine object exists).
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
        propagates. Counts failures within a sliding window; once the count
        reaches `max_restarts` the supervisor logs and returns rather than
        restarting, so a crash loop cannot spin the loop unbounded.
        """
        failures = 0
        window_start = time.monotonic()
        delay = backoff
        while True:
            started = time.monotonic()
            try:
                # The factory may do synchronous setup before building the
                # coroutine; if THAT raises it is a crash like any other and is
                # restarted, not propagated out of the supervisor.
                awaitable = coro_factory()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - logged and restarted
                self.logger.error("Supervised task %r crashed; will restart", name, exc_info=exc)
            else:
                # A non-awaitable RETURN is a programmer error (the contract
                # requires a fresh coroutine each call), not a crash: fail fast
                # and surface it rather than retrying to the breaker.
                if not inspect.isawaitable(awaitable):
                    raise TypeError(
                        f"app.supervise() factory for {name!r} must return a fresh "
                        f"awaitable on each call (e.g. `lambda: worker()`); got "
                        f"{type(awaitable).__name__}."
                    )
                try:
                    await awaitable
                except asyncio.CancelledError:
                    # Shutdown / explicit cancel - propagate so the spawned task
                    # drains cleanly and is not "restarted" into a new coroutine.
                    raise
                except BaseException as exc:  # noqa: BLE001 - logged and restarted
                    self.logger.error(
                        "Supervised task %r crashed; will restart", name, exc_info=exc
                    )
                else:
                    # A normal return is still treated as "needs restarting": a
                    # supervised task is meant to run for the app's lifetime, so a
                    # silent exit is logged rather than left dead.
                    self.logger.warning("Supervised task %r returned; restarting", name)
            now = time.monotonic()
            # A run that lasted longer than the window is a clean run: reset the
            # failure count and the backoff so only tight crash loops accumulate.
            if now - started >= restart_window:
                failures = 0
                window_start = now
                delay = backoff
            else:
                # Slide the window forward when it has elapsed since the first
                # counted failure, so failures spaced further apart than the
                # window never trip the breaker.
                if now - window_start >= restart_window:
                    failures = 0
                    window_start = now
                    delay = backoff
                failures += 1
                # `max_restarts` counts RESTARTS, not failed runs: allow N
                # restarts, then give up on the (N+1)th failure. (`>=` here would
                # make `max_restarts=1` retry zero times - off by one.)
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

    def _spawned_task_done(self, task: asyncio.Task[Any]) -> None:
        """Done-callback: drop the strong ref and log any non-cancel failure."""
        name = task.get_name()
        if self._spawned_named.get(name) is task:
            del self._spawned_named[name]
        self._spawned_anon.discard(task)
        self._log_background_task_error(task)

    async def wait_for_background_tasks(self, timeout: float | None = 5.0) -> bool:
        """Wait for every currently-spawned background task to finish.

        Returns `True` when they all completed, `False` when `timeout` elapsed
        first. Tasks are left running either way - this waits, it does not
        cancel; `_drain_spawned_tasks` is the shutdown path that does.

        A response's background task runs *after* the response is sent, which is
        the point of it, so a caller that needs its effect - a test asserting the
        email was queued, a script that must not exit early - otherwise has to
        guess at a sleep or drive the loop by hand. Newly spawned tasks are
        picked up too: a task that spawns another is waited for in full.

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

    async def _drain_spawned_tasks(self) -> None:
        """Cancel and await every spawned task within the per-task budget.

        Run twice by the shutdown lifecycle: once before the `on_shutdown`
        handlers, and again in a `finally` after they and the lifespan stack have
        unwound - so a task spawned by a teardown callback is drained too rather
        than surviving past shutdown. Each task gets at most
        `GRACEFUL_TASK_TIMEOUT` seconds to finish cancelling; a task that ignores
        cancellation past that is abandoned so shutdown cannot hang
        indefinitely.
        """
        tasks = [*self._spawned_named.values(), *self._spawned_anon]
        if not tasks:
            return
        timeout = self.config.get("GRACEFUL_TASK_TIMEOUT", 10)
        for task in tasks:
            task.cancel()
        # `wait` never raises for a task that errored or was cancelled - it just
        # reports it done - so the drain observes completion without re-raising
        # per-task failures (already logged by the done-callback). A task that
        # ignores cancellation past the budget lands in `pending` and is left
        # behind rather than hanging shutdown.
        await asyncio.wait(tasks, timeout=timeout)
        self._spawned_named.clear()
        self._spawned_anon.clear()
