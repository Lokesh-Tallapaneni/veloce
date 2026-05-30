"""Gunicorn worker class that serves a Veloce app via the raw HTTP protocol.

This is an **advanced, optional** alternative to running Veloce under
uvicorn. It lets gunicorn manage process supervision (forking, restarts,
signals) while each worker drives Veloce's own ``HttpProtocol`` directly on
an asyncio event loop — no uvicorn, no ASGI shim. uvicorn remains the
recommended production default; reach for this only when you already run a
gunicorn-based stack and want Veloce to slot into it.

gunicorn is **POSIX-only** and an optional dependency. Install it with::

    pip install veloceframework[gunicorn]

Then point gunicorn at the worker class by import path::

    gunicorn your_module:app -k veloce.workers.VeloceWorker

Importing this module (or ``import veloce``) never requires gunicorn — the
gunicorn base class is imported lazily, and the worker class only demands it
at instantiation, raising a clear :class:`ImportError` with an install hint
when it is missing.
"""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from veloce.app import Veloce

# gunicorn is optional and POSIX-only. Import its worker base at module load
# so the subclass below can extend it, but tolerate its absence: a box without
# gunicorn must still be able to `import veloce` and `import veloce.workers`.
# When unavailable we fall back to a plain `object` base and gate the real
# requirement behind instantiation, where a precise ImportError is raised. The
# base is typed `Any` so the subclass type-checks identically whether or not
# gunicorn is installed in the analysing environment.
try:
    from gunicorn.workers.base import Worker as _ImportedWorker

    _GUNICORN_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without gunicorn
    _ImportedWorker = object
    _GUNICORN_IMPORT_ERROR = exc

# Subclassing an `Any`-typed base keeps the worker checkable with and without
# gunicorn present; the runtime base is whichever the try/except selected.
_GunicornWorker: Any = _ImportedWorker


_INSTALL_HINT = (
    "VeloceWorker requires gunicorn, which is an optional, POSIX-only "
    "dependency. Install it with: pip install veloceframework[gunicorn]"
)


def build_protocol_factory(app: Veloce, loop: asyncio.AbstractEventLoop) -> functools.partial[Any]:
    """Return a zero-argument factory that builds an ``HttpProtocol``.

    asyncio's ``create_server`` calls the factory once per accepted
    connection. Binding the app and loop up front keeps the per-connection
    path allocation-light. Factored out so it can be unit-tested without a
    running loop or gunicorn present.
    """
    from veloce.serving.protocol import HttpProtocol

    return functools.partial(HttpProtocol, app, loop)


class VeloceWorker(_GunicornWorker):
    """Gunicorn worker that runs a Veloce app on its own asyncio loop.

    Subclasses gunicorn's worker base and bridges the master-bound listening
    socket(s) to an asyncio server whose protocol factory yields Veloce's
    ``HttpProtocol`` — the same raw HTTP/1.1 protocol ``Veloce.run()`` uses,
    bypassing ASGI entirely. gunicorn owns process supervision; this worker
    owns the event loop and the request pipeline.

    Use by import path on the gunicorn command line::

        gunicorn your_module:app -k veloce.workers.VeloceWorker

    POSIX/gunicorn-only. uvicorn remains the recommended production server;
    this is an advanced alternative for gunicorn-based deployments.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if _GUNICORN_IMPORT_ERROR is not None:
            raise ImportError(_INSTALL_HINT) from _GUNICORN_IMPORT_ERROR
        super().__init__(*args, **kwargs)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None

    def init_process(self) -> None:
        """Set up a fresh event loop, then hand control to the base class.

        gunicorn calls this once after the worker process is forked. A new
        loop is created and installed before ``super().init_process()`` runs
        the worker boot sequence (which ends by calling ``run()``), so the
        loop is current for the whole worker lifetime.
        """
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        super().init_process()

    def run(self) -> None:
        """Drive the event loop until gunicorn signals the worker to stop."""
        loop = self._loop
        if loop is None:  # pragma: no cover - init_process always sets it
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
        try:
            loop.run_until_complete(self._serve(loop))
        finally:
            loop.run_until_complete(self._shutdown())
            loop.close()

    def _veloce_app(self) -> Veloce:
        """Return the loaded Veloce application object.

        gunicorn's boot sequence loads the target callable during
        ``init_process`` and stores it on ``self.wsgi`` (``self.app`` is
        gunicorn's own Application wrapper, not the user's app). Fall back to
        ``self.app`` only if ``wsgi`` was not populated, which should not
        happen under a normal worker lifecycle.
        """
        return getattr(self, "wsgi", None) or self.app

    async def _serve(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the gunicorn sockets to an asyncio server and serve.

        gunicorn binds the listening socket(s) in the master and passes them
        down to each worker as ``self.sockets`` (objects exposing a ``.sock``
        attribute). Each is handed to ``loop.create_server`` so the kernel
        accept queue is shared across workers, matching gunicorn's pre-fork
        model. The loop runs until ``self.alive`` is cleared (by gunicorn's
        signal handlers) or a graceful timeout elapses with no requests.
        """
        app = self._veloce_app()

        await app._run_lifecycle("startup")

        factory = build_protocol_factory(app, loop)
        # gunicorn already created and bound the sockets in the master; reuse
        # them rather than binding fresh ones so all workers share one accept
        # queue. `sock=` takes the existing socket(s) directly.
        raw_socks = [gsock.sock for gsock in self.sockets]
        self._server = await loop.create_server(factory, sock=raw_socks[0])
        # A worker may be handed more than one bound socket (multiple binds).
        # create_server takes a single socket, so serve the rest explicitly.
        extra_servers = [
            await loop.create_server(factory, sock=gsock.sock) for gsock in self.sockets[1:]
        ]

        # gunicorn watches a per-worker heartbeat: notify() must be called
        # within `timeout` or the master kills the worker as hung. The loop
        # below pings it on a fixed cadence while the worker is alive.
        notify_interval = max(1.0, self.timeout / 2.0) if self.timeout else 1.0
        try:
            while self.alive:
                self.notify()
                await asyncio.sleep(notify_interval)
        finally:
            self._server.close()
            for server in extra_servers:
                server.close()
            await self._server.wait_closed()
            for server in extra_servers:
                await server.wait_closed()

    async def _shutdown(self) -> None:
        """Drain in-flight dispatch tasks, then run shutdown lifecycle.

        Mirrors ``Veloce._graceful_shutdown``: give active per-request tasks a
        bounded window to finish, cancel any stragglers, then run the app's
        shutdown hooks so resources opened at startup are released.
        """
        from veloce.serving.protocol import HttpProtocol

        app = self._veloce_app()

        if HttpProtocol._active_tasks:
            await asyncio.wait(HttpProtocol._active_tasks, timeout=self.timeout or 30)
        for task in HttpProtocol._active_tasks:
            task.cancel()
        HttpProtocol._active_tasks.clear()

        await app._run_lifecycle("shutdown")
