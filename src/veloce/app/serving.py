"""Native development server — the `app.run()` path and its shutdown handling.

Mixed into `Veloce`. These run the built-in asyncio/`HttpProtocol` server (not
ASGI - that path lives in `app.__call__`), so they are cold relative to request
serving. Lifecycle startup/shutdown is delegated back to the host via
`self._run_lifecycle`.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import ssl
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from veloce._protocol_constants import (
    LIFECYCLE_SHUTDOWN,
    LIFECYCLE_STARTUP,
    URL_SCHEME_HTTP,
    URL_SCHEME_HTTPS,
)

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app.core import Veloce


class ServingMixin:
    """Run and gracefully stop the built-in development server."""

    __slots__ = ()

    if TYPE_CHECKING:  # pragma: no cover
        # Attributes / methods the host application (`Veloce`) provides.
        config: Any
        logger: Any
        debug: bool
        version: str
        _run_lifecycle: Callable[..., Any]
        _stop_watchdog: Callable[..., Any]
        _shutdown_subapps: Callable[..., Any]
        _drain_spawned_tasks: Callable[..., Any]
        _setup_openapi: Callable[..., Any]
        _instrumentation: Any

    def run(
        self,
        host: str | None = None,
        port: int = 8000,
        workers: int = 1,
        access_log: bool = True,
        ssl_context: ssl.SSLContext | None = None,
        bind_all: bool = False,
        reload: bool = False,
    ) -> None:
        """Start the built-in **development** server.

        Veloce's from-scratch HTTP server is intended for local
        development only. For production, run the app under a hardened
        ASGI server - ``uvicorn your_module:app`` - which veloce is fully
        compatible with through its ASGI ``__call__`` interface.
        ``run()`` logs a reminder of this on startup.

        ``host`` resolves to ``"127.0.0.1"`` when unset so the dev server
        is reachable only from the local machine. Pass ``bind_all=True``
        to opt in to all-interfaces binding (``"0.0.0.0"``). ``host`` and
        ``bind_all=True`` are mutually exclusive - passing both raises
        ``ValueError`` to avoid silent privilege widening. Binding to
        ``0.0.0.0`` exposes the dev server to every reachable network -
        including remote attackers if the machine is on a public network
        - so it should be used only in trusted environments and never
        with ``debug=True``.

        ``ssl_context`` - an ``ssl.SSLContext`` - turns on HTTPS for local
        testing; it is handed straight to ``loop.create_server(ssl=...)``.
        Left ``None`` (the default) the serving path is byte-for-byte the
        same as plain HTTP. Production should still terminate TLS at
        uvicorn or a reverse proxy.

        ``workers`` must be ``1``: the built-in server runs a single process
        and does not pre-fork. Passing more raises ``ValueError`` - run under
        ``uvicorn module:app --workers N`` or the gunicorn ``VeloceWorker`` for
        multiple processes.

        ``reload=True`` turns on the development auto-reloader: this process
        supervises a child that serves requests and restarts it whenever a
        project ``.py`` file changes. The watching happens in the supervisor, so
        the served child carries no overhead. It is a development aid - leave it
        off for any deployment.
        """
        if host is not None and bind_all:
            raise ValueError(
                "Veloce.run: bind_all=True conflicts with explicit host=...; pass only one"
            )
        # The built-in server runs in a single process - it does not pre-fork
        # (cross-platform pre-forking needs SO_REUSEPORT, absent on Windows).
        # Silently accepting workers>1 and running one process is a footgun, so
        # reject it and point at the multi-process production paths.
        if workers != 1:
            raise ValueError(
                "Veloce.run(workers=...) runs a single process; the built-in "
                "development server does not spawn workers. For multiple "
                "processes run under an ASGI server (uvicorn module:app "
                "--workers N) or the gunicorn VeloceWorker."
            )

        # Auto-reload: the supervisor process (no child marker set) hands off to
        # the watch loop, which re-spawns this same command as a serving child
        # on every source change. The child sees the marker and falls straight
        # through to serve. Deferred import keeps the reloader out of a normal
        # run() entirely, so reload=False costs nothing.
        if reload:
            from veloce.serving.reloader import is_reloader_child, run_with_reloader

            if not is_reloader_child():
                run_with_reloader()
                return

        if host is None:
            host = "0.0.0.0" if bind_all else "127.0.0.1"
        self._setup_openapi()

        # The from-scratch server is dev-grade - make the production
        # recommendation impossible to miss.
        self.logger.warning(
            "veloce's built-in server (app.run()) is for local development only - "
            "run under uvicorn (or another hardened ASGI server) in production."
        )

        # Debug tracebacks leak source and internals - binding a non-local
        # host with debug=True exposes them to the network.
        if self.debug and host not in ("127.0.0.1", "::1", "localhost"):
            self.logger.warning(
                "debug=True with a non-local bind (host=%r) exposes debug "
                "tracebacks to the network - set debug=False for any deployment "
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
            scheme = URL_SCHEME_HTTPS if ssl_context is not None else URL_SCHEME_HTTP
            print(f"\n  Veloce v{self.version}")
            print(f"  Listening on {scheme}://{host}:{port}")
            print("  Press Ctrl+C to stop\n")
            # The flag named itself after this and only printed the banner: a
            # development server that answers requests silently is the odd one
            # out, and a request that fails leaves nothing to correlate it with.
            # Only the built-in server installs it - under an ASGI server that
            # server writes the access log, and a second one would duplicate it.
            self._install_dev_access_log()

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

    def _install_dev_access_log(self) -> None:
        """Register the per-request access log for the built-in server.

        A no-op when the application already registered one, so `run()` never
        doubles up on an app that called `instrument_access_log` itself.
        """
        # Deferred: `observability` imports from the app package, so hoisting
        # this would circle at import time.
        from veloce.observability import instrument_access_log

        for hook in self._instrumentation:
            if getattr(hook, "__module__", None) == "veloce.observability":
                return
        instrument_access_log(cast("Veloce", self))

    async def _serve(self, host: str, port: int, access_log: bool, ssl_context: Any = None) -> None:
        """Create the server and run forever."""
        # Deferred: serving.protocol imports `veloce.status`, which triggers
        # `veloce/__init__` -> back to this app module. Hoisting would
        # circle at package import time. Both call sites below share the
        # same break.
        from veloce.serving.protocol import HttpProtocol

        loop = asyncio.get_running_loop()
        # Run startup hooks
        await self._run_lifecycle(LIFECYCLE_STARTUP)

        # `SO_REUSEPORT` is absent on Windows (and some others); the stdlib
        # selector loop raises `ValueError: reuse_port not supported by socket
        # module` if `reuse_port=True` is passed there, killing the serving
        # thread before it binds. Request it only where the socket option
        # exists, so the native server starts on every supported platform.
        reuse_port = True if hasattr(socket, "SO_REUSEPORT") else None
        # `ssl=None` (the default) makes `create_server` behave exactly as
        # the plain-HTTP path; TLS cost is paid only when a context is set.
        server = await loop.create_server(
            # `self` is always a `Veloce` (this mixin is only composed into it).
            lambda: HttpProtocol(self, loop),  # type: ignore[arg-type]
            host,
            port,
            reuse_port=reuse_port,
            ssl=ssl_context,
        )
        # Handle signals for graceful shutdown
        shutdown_event = asyncio.Event()

        def _signal_handler() -> None:
            server.close()
            shutdown_event.set()

        # POSIX: the loop installs the handler and runs it on the loop thread.
        native_signals = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                # Windows: `loop.add_signal_handler` is unsupported. Without a
                # handler, Ctrl+C / Ctrl+Break raise `KeyboardInterrupt` straight
                # out of the loop, tearing down in-flight connections before the
                # graceful drain can run. Fall back to `signal.signal` (which
                # replaces the default KeyboardInterrupt-raising handler) and
                # bounce the cooperative shutdown onto the loop thread, so an
                # in-flight request drains at its own boundary.
                native_signals = False
                break

        # Windows fallback: `signal.signal` installs a PROCESS-WIDE handler, so
        # the previous handlers are saved and restored after serving - otherwise a
        # handler closing over this (soon-closed) loop leaks past `run()` and a
        # later Ctrl+C would schedule onto a dead loop.
        restore_signals: list[tuple[int, Any]] = []
        if not native_signals:

            def _os_signal_handler(signum: int, frame: Any) -> None:
                # A late console control event can arrive once the loop is already
                # closing/closed; ignore it rather than raising on a closed loop.
                if not loop.is_closed():
                    loop.call_soon_threadsafe(_signal_handler)

            win_signals = [signal.SIGINT]
            if hasattr(signal, "SIGBREAK"):
                win_signals.append(signal.SIGBREAK)
            for sig in win_signals:
                previous = signal.getsignal(sig)
                try:
                    signal.signal(sig, _os_signal_handler)
                except (ValueError, OSError):
                    # `signal.signal` only works on the main thread; if `run()` is
                    # driven from another thread, skip it (shutdown then relies on
                    # the main thread / explicit stop).
                    continue
                restore_signals.append((sig, previous))

        try:
            async with server:
                if native_signals:
                    await shutdown_event.wait()
                else:
                    # Windows: wake periodically so a signal-scheduled shutdown is
                    # observed promptly even when the loop was otherwise idle when
                    # the console control event arrived.
                    while not shutdown_event.is_set():
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(shutdown_event.wait(), 0.25)
                # Quiesce live connections while still INSIDE the context
                # manager: leaving it runs `close()` + `await wait_closed()`,
                # and since Python 3.12 that really waits for every accepted
                # connection. An idle keep-alive client would otherwise hold
                # shutdown for the full KEEP_ALIVE_TIMEOUT before
                # `_graceful_shutdown` ever got the chance to drain it.
                HttpProtocol.start_graceful_drain()
        finally:
            for sig_num, previous in restore_signals:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig_num, previous)

    async def _graceful_shutdown(self, loop: asyncio.AbstractEventLoop) -> None:
        """Two-phase graceful shutdown, then run the shutdown lifecycle.

        Phase one quiesces every live connection: each finishes the request it
        is already dispatching and then closes at the request boundary instead
        of being cancelled mid-pipeline. A connection accepted in the shutdown
        window serves at most its first request. Phase two is the existing hard
        fallback - any dispatch still running past the drain window is awaited
        with a timeout, then cancelled - so a stuck handler can never hang the
        process.
        """
        # Deferred: same `veloce.status` -> `veloce/__init__` cycle that
        # the matching import in `_serve` breaks. These are the only two
        # call sites; not worth a structural refactor.
        from veloce.serving.protocol import HttpProtocol

        # Phase one: flip every live connection's drain flag so each self-
        # quiesces at its own request boundary - no abrupt mid-pipeline cancel.
        HttpProtocol.start_graceful_drain()

        # Phase two (hard fallback): give in-flight dispatch tasks a bounded
        # window to finish draining, then cancel any straggler so shutdown
        # cannot block forever on a handler that ignores the drain.
        if HttpProtocol._active_tasks:
            await asyncio.wait(
                HttpProtocol._active_tasks,
                timeout=30,
            )

        # Cancel any still-running tasks
        for task in HttpProtocol._active_tasks:
            task.cancel()
        HttpProtocol._active_tasks.clear()

        # Clear the process-wide drain latch. Shutdown is terminal in
        # production, but a single interpreter that serves again (notably the
        # test harness) must not inherit a stuck "draining" state.
        HttpProtocol.reset_graceful_drain()

        # Run shutdown lifecycle hooks
        await self._run_lifecycle(LIFECYCLE_SHUTDOWN)
