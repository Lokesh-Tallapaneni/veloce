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

**TLS:** when gunicorn is started with ``--certfile`` / ``--keyfile``
(``cfg.is_ssl``), the worker builds a server SSL context from gunicorn's
config and hands it to ``create_server``; if the certificate chain is missing
or unloadable it fails fast with :class:`RuntimeError` rather than silently
serving cleartext over an HTTPS deployment. The default context is passed
through gunicorn's ``ssl_context(config, default_ssl_context_factory)`` hook,
so a deployment that customises TLS there (minimum TLS version, mTLS tweaks)
has those customisations honoured.

**EXPERIMENTAL:** this worker is new and the gunicorn integration cannot be
exercised on Windows (gunicorn is POSIX-only). Validate it on a POSIX host
under your real gunicorn configuration before relying on it in production;
uvicorn remains the recommended default.
"""

from __future__ import annotations

import asyncio
import functools
import os
import ssl
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


def build_ssl_context(ssl_options: dict[str, Any]) -> ssl.SSLContext:
    """Build a server ``ssl.SSLContext`` from gunicorn's ``cfg.ssl_options``.

    gunicorn exposes the resolved TLS settings as a flat dict
    (``keyfile``, ``certfile``, ``ssl_version``, ``cert_reqs``, ``ca_certs``,
    ``ciphers``, plus the wrap-time flags). This mirrors gunicorn's own
    server-side context construction closely enough that handing the result to
    ``loop.create_server(ssl=...)`` terminates TLS the same way gunicorn's
    sync/threaded workers do.

    A ``certfile`` is required: ``cfg.is_ssl`` is true when *either* a cert or
    key is set, but a usable server context needs the certificate chain. If it
    is missing this raises ``RuntimeError`` so the worker fails fast rather than
    silently serving cleartext. Factored out to be unit-testable without
    gunicorn or a running loop.
    """
    certfile = ssl_options.get("certfile")
    keyfile = ssl_options.get("keyfile")
    if not certfile:
        raise RuntimeError(
            "VeloceWorker: gunicorn TLS is configured (is_ssl) but no certfile "
            "was provided; refusing to start to avoid silently serving cleartext "
            "over an HTTPS deployment. Pass gunicorn --certfile (and --keyfile)."
        )

    context = ssl.create_default_context(
        ssl.Purpose.CLIENT_AUTH, cafile=ssl_options.get("ca_certs")
    )
    # `create_default_context(CLIENT_AUTH)` defaults to verify_mode=CERT_NONE,
    # which is correct for a TLS server that does not require client certs;
    # honour an explicit gunicorn cert_reqs (e.g. mutual TLS) when set.
    cert_reqs = ssl_options.get("cert_reqs")
    if cert_reqs is not None:
        context.verify_mode = ssl.VerifyMode(cert_reqs)

    try:
        context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    except (OSError, ssl.SSLError) as exc:
        raise RuntimeError(
            f"VeloceWorker: failed to load TLS cert chain "
            f"(certfile={certfile!r}, keyfile={keyfile!r}): {exc}"
        ) from exc

    ciphers = ssl_options.get("ciphers")
    if ciphers:
        context.set_ciphers(ciphers)

    return context


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
        # Set when gunicorn asks the worker to stop, so the serve loop reacts
        # immediately instead of waiting out its heartbeat sleep.
        self._stop = asyncio.Event()
        # Snapshot the parent (arbiter) pid at construction. If the master dies
        # the worker is reparented (getppid() changes, typically to 1/init), and
        # the heartbeat loop uses this to stop instead of orphaning. gunicorn's
        # base sets self.ppid in __init__; fall back to os.getppid() defensively.
        self._initial_ppid: int = getattr(self, "ppid", None) or os.getppid()

    def _request_stop(self) -> None:
        """Wake the serve loop from gunicorn's signal handler thread."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._stop.set)

    def _parent_alive(self) -> bool:
        """Best-effort check that the gunicorn master is still our parent.

        When the arbiter dies the kernel reparents this worker (``getppid()``
        changes, typically to 1/init), so a changed parent pid means the master
        is gone and the worker should stop rather than orphan. Guarded: any
        failure reading the pid is treated as "still alive" so a transient error
        never tears the worker down spuriously.
        """
        try:
            return os.getppid() == self._initial_ppid
        except OSError:  # pragma: no cover - getppid does not normally fail
            return True

    def _count_request(self) -> None:
        """Increment the handled-request counter and trip max_requests recycling.

        Installed as ``HttpProtocol.on_request_complete`` so it fires once per
        dispatched request. gunicorn's base computes ``self.max_requests`` with
        any ``max_requests_jitter`` already folded in (and uses ``sys.maxsize``
        when recycling is disabled), so a plain ``>=`` comparison matches its
        documented behaviour. Clearing ``self.alive`` lets the master replace
        the worker after the in-flight request drains; waking ``_stop`` makes the
        heartbeat loop notice immediately.
        """
        self.nr += 1
        max_requests = getattr(self, "max_requests", 0)
        if max_requests and self.nr >= max_requests:
            self.alive = False
            self._stop.set()

    def _keep_serving(self) -> bool:
        """Report whether a connection may serve its next queued request.

        Installed as ``HttpProtocol.should_keep_serving`` and consulted by the
        per-connection serve loop after each dispatched request. Returning
        ``self.alive`` makes the loop stop at the request boundary once
        ``max_requests`` recycling has cleared ``alive`` — otherwise a single
        connection with queued/pipelined requests would keep draining them past
        the limit before the worker restarts. gunicorn's own workers flip
        ``alive`` and break inside the request-handling path for the same
        reason.
        """
        return self.alive

    def handle_exit(self, sig: Any, frame: Any) -> None:
        # SIGTERM/SIGINT: gunicorn clears self.alive; wake the loop too so the
        # worker stops within a scheduler tick rather than up to a heartbeat.
        self._request_stop()
        super().handle_exit(sig, frame)

    def handle_quit(self, sig: Any, frame: Any) -> None:
        self._request_stop()
        super().handle_quit(sig, frame)

    def init_process(self) -> None:
        """Set up a fresh event loop, then hand control to the base class.

        gunicorn calls this once after the worker process is forked. A new
        loop is created and installed before ``super().init_process()`` runs
        the worker boot sequence (which ends by calling ``run()``), so the
        loop is current for the whole worker lifetime.
        """
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        # Refresh the parent-pid baseline post-fork: the arbiter pid passed to
        # __init__ is the live master, and getppid() in the forked worker should
        # match it, but recapture so the liveness check compares against the
        # actual parent of this process.
        self._initial_ppid = os.getppid()
        # Drive gunicorn --max-requests recycling: HttpProtocol calls this hook
        # once per dispatched request. Set on the protocol class (process-wide);
        # each forked worker installs its own bound method, and only one worker
        # runs per process so there is no cross-worker contention.
        from veloce.serving.protocol import HttpProtocol

        HttpProtocol.on_request_complete = self._count_request
        HttpProtocol.should_keep_serving = self._keep_serving
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

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """Return a server SSL context when gunicorn was started with TLS.

        Reads ``cfg.is_ssl`` / ``cfg.ssl_options`` from gunicorn's config. When
        TLS is off (the common case) this returns ``None`` and the serving path
        is plain HTTP, byte-for-byte as before.

        When TLS is on, the default context is built from ``cfg.ssl_options``
        via :func:`build_ssl_context` (which fails fast if the cert chain is
        missing — never silently downgrading HTTPS to cleartext). That default
        is then run through gunicorn's documented ``ssl_context(config,
        default_ssl_context_factory)`` hook, mirroring how gunicorn's own socket
        layer calls ``conf.ssl_context(conf, default_ssl_context_factory)``. A
        deployment that customises TLS in this hook (minimum TLS version, mTLS
        tweaks, ciphers) sees those customisations honoured here; the stock hook
        just returns ``default_ssl_context_factory()`` unchanged.
        """
        cfg = getattr(self, "cfg", None)
        if cfg is None:  # pragma: no cover - gunicorn always sets cfg
            return None
        if not getattr(cfg, "is_ssl", False):
            return None
        ssl_options = dict(getattr(cfg, "ssl_options", None) or {})

        def default_ssl_context_factory() -> ssl.SSLContext:
            return build_ssl_context(ssl_options)

        hook = getattr(cfg, "ssl_context", None)
        if not callable(hook):
            return default_ssl_context_factory()

        context = hook(cfg, default_ssl_context_factory)
        if not isinstance(context, ssl.SSLContext):
            raise RuntimeError(
                "VeloceWorker: the configured gunicorn ssl_context hook returned "
                f"{type(context).__name__}, not an ssl.SSLContext; it must return "
                "the (optionally customised) context from default_ssl_context_factory()."
            )
        return context

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
        if not self.sockets:
            raise RuntimeError(
                "VeloceWorker received no listening sockets from gunicorn; "
                "ensure a bind address is configured (e.g. gunicorn --bind)."
            )
        # If gunicorn was started with TLS (--certfile/--keyfile, cfg.is_ssl),
        # build a server SSL context and pass it to create_server. Without this
        # the worker would hand the bound sockets to asyncio with no TLS and an
        # HTTPS deployment would silently serve cleartext. build_ssl_context
        # raises if the cert chain is missing/unloadable, so the worker fails
        # fast rather than downgrading the security posture.
        ssl_context = self._build_ssl_context()

        raw_socks = [gsock.sock for gsock in self.sockets]
        self._server = await loop.create_server(factory, sock=raw_socks[0], ssl=ssl_context)
        # A worker may be handed more than one bound socket (multiple binds).
        # create_server takes a single socket, so serve the rest explicitly.
        extra_servers = [
            await loop.create_server(factory, sock=gsock.sock, ssl=ssl_context)
            for gsock in self.sockets[1:]
        ]

        # gunicorn watches a per-worker heartbeat: notify() must be called
        # within `timeout` or the master kills the worker as hung. The loop
        # below pings it on a fixed cadence while the worker is alive.
        notify_interval = max(1.0, self.timeout / 2.0) if self.timeout else 1.0
        try:
            while self.alive and self._parent_alive():
                self.notify()
                try:
                    # Wait for a stop signal, but wake at least every
                    # notify_interval to ping gunicorn's heartbeat AND re-check
                    # arbiter liveness. If the stop event fires (SIGTERM/SIGQUIT,
                    # or max_requests recycling) the wait returns at once; if the
                    # signal hook is ever missed, the timeout still re-checks
                    # self.alive and the parent pid — so a dead master no longer
                    # leaves the worker orphaned, and this never reacts slower
                    # than the previous fixed-sleep loop.
                    await asyncio.wait_for(self._stop.wait(), timeout=notify_interval)
                except asyncio.TimeoutError:
                    continue
                break
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

        # Detach our max_requests hooks so a stopped worker leaves no dangling
        # reference on the process-wide protocol class (matters for the test
        # harness, where many workers may share an interpreter).
        if HttpProtocol.on_request_complete == self._count_request:
            HttpProtocol.on_request_complete = None
        if HttpProtocol.should_keep_serving == self._keep_serving:
            HttpProtocol.should_keep_serving = None

        app = self._veloce_app()

        if HttpProtocol._active_tasks:
            await asyncio.wait(HttpProtocol._active_tasks, timeout=self.timeout or 30)
        for task in HttpProtocol._active_tasks:
            task.cancel()
        HttpProtocol._active_tasks.clear()

        await app._run_lifecycle("shutdown")
