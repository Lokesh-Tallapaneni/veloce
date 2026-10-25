"""Native development server — the `app.run()` path and its shutdown handling.

Mixed into `Veloce`. These run the built-in asyncio/`HttpProtocol` server (not
ASGI - that path lives in `app.__call__`), so they are cold relative to request
serving. Lifecycle startup/shutdown is delegated back to the host via
`self._run_lifecycle`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import signal
import socket
import ssl
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, cast

from typing_extensions import Doc

from veloce._protocol_constants import (
    LIFECYCLE_SHUTDOWN,
    LIFECYCLE_STARTUP,
    URL_SCHEME_HTTP,
    URL_SCHEME_HTTPS,
)
from veloce._version import resolve_version
from veloce.app._host import AppHost

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app.core import Veloce

# Bounds for the accept queue, so an enormous advertised limit is not taken as is.
_MIN_LISTEN_BACKLOG = 512
_MAX_LISTEN_BACKLOG = 4096


@functools.lru_cache(maxsize=1)
def _resolve_listen_backlog() -> int:
    """Depth to request for the built-in server's accept queue.

    The kernel silently clamps the request to its own maximum, so ask the
    machine what it allows and stay inside it. asyncio's default of 100 is far
    below that: a burst of 5000 concurrent connects was measured establishing
    1000 connections against it.

    Memoized because the answer cannot change while the process runs, and
    reading it at import would charge every application a syscall for a server
    most never start.
    """
    try:
        with open("/proc/sys/net/core/somaxconn", "rb") as fh:
            limit = int(fh.read())
    except (OSError, ValueError):
        # No procfs, or an unparseable value: ask the ceiling, let the kernel clamp.
        limit = _MAX_LISTEN_BACKLOG
    return max(_MIN_LISTEN_BACKLOG, min(limit, _MAX_LISTEN_BACKLOG))


class ServingMixin(AppHost):
    """Run and gracefully stop the built-in development server."""

    def run(
        self,
        host: Annotated[
            str | None,
            Doc("Interface to bind; defaults to `127.0.0.1`. Conflicts with `bind_all`."),
        ] = None,
        port: Annotated[int, Doc("TCP port to listen on.")] = 8000,
        workers: Annotated[int, Doc("Must be `1`; the built-in server runs a single process.")] = 1,
        access_log: Annotated[
            bool, Doc("Print the start-up banner and install the development access log.")
        ] = True,
        ssl_context: Annotated[
            ssl.SSLContext | None, Doc("Serve HTTPS for local testing when given.")
        ] = None,
        bind_all: Annotated[
            bool, Doc("Bind every interface (`0.0.0.0`) instead of localhost.")
        ] = False,
        reload: Annotated[
            bool, Doc("Restart the server whenever a project `.py` file changes.")
        ] = False,
    ) -> None:
        """Start the built-in **development** server.

        This from-scratch HTTP server is for local development only; in
        production run the app under a hardened ASGI server
        (``uvicorn your_module:app``), which its ASGI ``__call__`` supports.

        ``host`` resolves to ``"127.0.0.1"`` when unset, and is mutually
        exclusive with ``bind_all=True`` - passing both raises ``ValueError``
        rather than silently widening privilege. ``0.0.0.0`` exposes the dev
        server to every reachable network, so use it only in a trusted
        environment and never with ``debug=True``.

        ``ssl_context`` is handed straight to ``loop.create_server(ssl=...)``;
        left ``None``, the serving path is byte-for-byte the plain-HTTP one.

        ``workers`` must be ``1`` - the built-in server does not pre-fork, so
        anything else raises ``ValueError``.

        ``reload=True`` makes this process supervise a serving child and restart
        it on any project ``.py`` change; the watching stays in the supervisor,
        so the child carries no overhead.
        """
        if host is not None and bind_all:
            raise ValueError(
                "Veloce.run: bind_all=True conflicts with explicit host=...; pass only one"
            )
        # No pre-forking: it would need SO_REUSEPORT, absent on Windows.
        if workers != 1:
            raise ValueError(
                "Veloce.run(workers=...) runs a single process; the built-in "
                "development server does not spawn workers. For multiple "
                "processes run under an ASGI server (uvicorn module:app "
                "--workers N) or the gunicorn VeloceWorker."
            )

        # The supervisor re-spawns this command as a serving child on each change.
        if reload:
            from veloce.serving.reloader import is_reloader_child, run_with_reloader

            if not is_reloader_child():
                run_with_reloader()
                return

        if host is None:
            host = "0.0.0.0" if bind_all else "127.0.0.1"
        self._setup_openapi()

        self.logger.warning(
            "veloce's built-in server (app.run()) is for local development only - "
            "run under uvicorn (or another hardened ASGI server) in production."
        )

        if self.debug and host not in ("127.0.0.1", "::1", "localhost"):
            self.logger.warning(
                "debug=True with a non-local bind (host=%r) exposes debug "
                "tracebacks to the network - set debug=False for any deployment "
                "reachable beyond localhost.",
                host,
            )

        # A loop factory, not the deprecated event-loop policy API. uvloop is an
        # optional extra, absent on Windows, so the ImportError is ordinary there.
        new_loop = asyncio.new_event_loop
        try:
            import uvloop  # pyright: ignore[reportMissingImports]

            new_loop = uvloop.new_event_loop
        except ImportError:
            pass

        if access_log:
            self._print_banner(host, port, tls=ssl_context is not None)
            # Only the built-in server installs this; an ASGI server writes its own.
            self._install_dev_access_log()

        loop = new_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(self._serve(host, port, ssl_context))
        except KeyboardInterrupt:
            pass
        finally:
            loop.run_until_complete(self._graceful_shutdown(loop))
            loop.close()

    def _print_banner(self, host: str, port: int, tls: bool = False) -> None:
        """Print the development server's start-up banner.

        The version is the installed *framework* version, not `self.version`,
        which is the API version the OpenAPI document carries.
        """
        scheme = URL_SCHEME_HTTPS if tls else URL_SCHEME_HTTP
        print(f"\n  Veloce v{resolve_version()}")
        print(f"  Listening on {scheme}://{host}:{port}")
        print("  Press Ctrl+C to stop\n")

    def _install_dev_access_log(self) -> None:
        """Register the per-request access log for the built-in server.

        A no-op when the application already registered one, so `run()` never
        doubles up on an app that called `instrument_access_log` itself.
        """
        # Deferred to keep the layering: `app/` is core, `observability` is not.
        from veloce.observability import instrument_access_log

        # Asked of the hook, not its module, so a user's own log suppresses this.
        for hook in self._instrumentation:
            if getattr(hook, "is_access_log", False):
                return
        instrument_access_log(cast("Veloce", self))

    @staticmethod
    def _install_shutdown_signals(
        loop: asyncio.AbstractEventLoop, on_shutdown: Callable[[], None]
    ) -> tuple[bool, list[tuple[int, Any]]]:
        """Arrange for `on_shutdown` to run when the process is asked to stop.

        Returns `(loop_owns_the_handlers, handlers_to_restore)`. Loop-installed
        handlers run on the loop thread and wake it, so `_serve` can wait
        indefinitely; the handlers to restore are non-empty only on the fallback.

        Windows has no `loop.add_signal_handler`, and without one Ctrl+C raises
        `KeyboardInterrupt` out of the loop, tearing down in-flight connections
        before the drain can run. The `signal.signal` fallback bounces the
        cooperative shutdown onto the loop thread instead.

        That handler is PROCESS-WIDE, so the previous ones are returned to be
        restored: one closing over this soon-closed loop would outlive `run()`.
        """
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, on_shutdown)
            except NotImplementedError:
                break
        else:
            return True, []

        def _os_signal_handler(signum: int, frame: Any) -> None:
            # A late console control event must not raise on a closed loop.
            if not loop.is_closed():
                loop.call_soon_threadsafe(on_shutdown)

        restore: list[tuple[int, Any]] = []
        win_signals = [signal.SIGINT]
        if hasattr(signal, "SIGBREAK"):
            win_signals.append(signal.SIGBREAK)
        for sig in win_signals:
            previous = signal.getsignal(sig)
            try:
                signal.signal(sig, _os_signal_handler)
            except (ValueError, OSError):
                # `signal.signal` is main-thread only; elsewhere, an explicit stop.
                continue
            restore.append((sig, previous))
        return False, restore

    @staticmethod
    def _restore_shutdown_signals(restore: list[tuple[int, Any]]) -> None:
        """Put back the process-wide handlers the fallback path replaced.

        Failures are suppressed: this runs while the process is already shutting
        down, and must not become the reason `run()` raises.
        """
        for sig_num, previous in restore:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig_num, previous)

    async def _serve(self, host: str, port: int, ssl_context: Any = None) -> None:
        """Create the server and run forever."""
        # Deferred for cost: hoisting added 11 modules and ~8ms to `import veloce`.
        from veloce.serving.protocol import HttpProtocol

        loop = asyncio.get_running_loop()
        await self._run_lifecycle(LIFECYCLE_STARTUP)

        # `reuse_port=True` raises out of the Windows selector loop, which has none.
        reuse_port = True if hasattr(socket, "SO_REUSEPORT") else None
        server = await loop.create_server(
            # `self` is always a `Veloce` (this mixin is only composed into it).
            lambda: HttpProtocol(self, loop),  # type: ignore[arg-type]
            host,
            port,
            backlog=_resolve_listen_backlog(),
            reuse_port=reuse_port,
            ssl=ssl_context,
        )
        shutdown_event = asyncio.Event()

        def _signal_handler() -> None:
            server.close()
            shutdown_event.set()

        native_signals, restore_signals = self._install_shutdown_signals(loop, _signal_handler)

        try:
            async with server:
                if native_signals:
                    await shutdown_event.wait()
                else:
                    # Windows: wake up, so a scheduled shutdown is seen when idle.
                    while not shutdown_event.is_set():
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(shutdown_event.wait(), 0.25)
                # Inside the manager: leaving awaits every connection on 3.12+.
                HttpProtocol.start_graceful_drain()
        finally:
            self._restore_shutdown_signals(restore_signals)

    async def _graceful_shutdown(self, loop: asyncio.AbstractEventLoop) -> None:
        """Two-phase graceful shutdown, then run the shutdown lifecycle.

        Phase one lets each live connection finish the request it is dispatching
        and close at the request boundary. Phase two awaits whatever is still
        running past the drain window and then cancels it, so a stuck handler
        can never hang the process.
        """
        # Deferred for the same import-cost reason as the one in `_serve`.
        from veloce.serving.protocol import HttpProtocol

        HttpProtocol.start_graceful_drain()

        # Configurable: the window must fit a grace period only the deployment knows.
        if HttpProtocol._active_tasks:
            await asyncio.wait(
                HttpProtocol._active_tasks,
                timeout=self.config.get("GRACEFUL_DRAIN_TIMEOUT", 30),
            )

        for task in HttpProtocol._active_tasks:
            task.cancel()
        HttpProtocol._active_tasks.clear()

        # Cleared so an interpreter that serves again does not inherit "draining".
        HttpProtocol.reset_graceful_drain()

        await self._run_lifecycle(LIFECYCLE_SHUTDOWN)
