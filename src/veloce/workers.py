"""Gunicorn worker — serves a Veloce app via the raw HTTP protocol.

An **advanced, optional** alternative to running Veloce under uvicorn:
gunicorn manages process supervision while each worker drives Veloce's own
`HttpProtocol` directly on an asyncio event loop, with no ASGI shim.

gunicorn is **POSIX-only** and an optional dependency. Install it with::

    pip install veloceframework[gunicorn]

Then point gunicorn at the worker class by import path::

    gunicorn your_module:app -k veloce.workers.VeloceWorker

Importing this module never requires gunicorn: the base class is imported
lazily and the worker demands it only at instantiation, with an install hint.

**TLS:** when gunicorn is started with `--certfile` / `--keyfile` (`cfg.is_ssl`)
the worker builds a server SSL context and hands it to `create_server`. A chain
that cannot be *loaded* fails fast with `RuntimeError` rather than silently
serving cleartext; an expired but well-formed chain only warns, since a
certificate can be renewed under a running server. The default context goes
through gunicorn's `ssl_context(config, default_ssl_context_factory)` hook so a
deployment customising TLS there is honoured, and `--ssl-version` is applied as
a minimum-version floor - never one below the interpreter default, which
RFC 8996 deprecates.

**Worker recycling:** `--max-requests` counts HTTP requests, which a WebSocket
has none of until it closes, so it is not a lifetime bound for a
WebSocket-heavy deployment.

**EXPERIMENTAL:** the gunicorn integration cannot be exercised on Windows;
validate it on a POSIX host under your real configuration first.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import ssl
import time
from typing import TYPE_CHECKING, Any

from veloce._protocol_constants import LIFECYCLE_SHUTDOWN, LIFECYCLE_STARTUP
from veloce.app.serving import _resolve_listen_backlog

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce

_logger = logging.getLogger(__name__)

# Its absence must still allow `import veloce.workers`; raised at instantiation.
try:
    from gunicorn.workers.base import Worker as _ImportedWorker

    _GUNICORN_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:  # pragma: no cover - exercised only without gunicorn
    _ImportedWorker = object
    _GUNICORN_IMPORT_ERROR = exc

# Typed `Any` so the subclass checks identically with and without gunicorn.
_GunicornWorker: Any = _ImportedWorker


_INSTALL_HINT = (
    "VeloceWorker requires gunicorn, which is an optional, POSIX-only "
    "dependency. Install it with: pip install veloceframework[gunicorn]"
)


def build_protocol_factory(app: Veloce, loop: asyncio.AbstractEventLoop) -> functools.partial[Any]:
    """Return a zero-argument factory that builds an `HttpProtocol`.

    `create_server` calls it once per accepted connection, so binding the app and
    loop up front keeps the per-connection path allocation-light.
    """
    # Deferred for import cost, not a cycle; same for the three sites below.
    from veloce.serving.protocol import HttpProtocol

    return functools.partial(HttpProtocol, app, loop)


# Via `getattr`, as the deprecated TLS 1.0 / 1.1 names may be absent.
_SSL_VERSION_FLOORS: dict[int, ssl.TLSVersion] = {
    value: floor
    for value, floor in (
        (getattr(ssl, "PROTOCOL_TLSv1", None), ssl.TLSVersion.TLSv1),
        (getattr(ssl, "PROTOCOL_TLSv1_1", None), ssl.TLSVersion.TLSv1_1),
        (getattr(ssl, "PROTOCOL_TLSv1_2", None), ssl.TLSVersion.TLSv1_2),
    )
    if value is not None
}


def _apply_ssl_version(context: ssl.SSLContext, ssl_version: Any) -> None:
    """Honour gunicorn's `ssl_version` as a minimum-version floor.

    A floor *below* the interpreter default is logged and refused rather than
    applied: RFC 8996 deprecates TLS 1.0 and 1.1, and silently weakening a
    server's handshake is worse than ignoring the setting.
    """
    if ssl_version is None:
        return
    floor = _SSL_VERSION_FLOORS.get(ssl_version)
    if floor is None:
        # `PROTOCOL_TLS*` means "negotiate the best available", the default.
        return
    if floor < context.minimum_version:
        _logger.warning(
            "VeloceWorker: ignoring --ssl-version %s; it would lower the TLS "
            "floor below this interpreter's default of %s (RFC 8996 deprecates "
            "TLS 1.0 and 1.1). Remove the option to silence this.",
            floor.name,
            context.minimum_version.name,
        )
        return
    context.minimum_version = floor


def _warn_if_chain_expired(certfile: str) -> None:
    """Log when the certificate is outside its validity window.

    `load_cert_chain` checks that a chain parses, not that it is valid, so
    without this every handshake fails looking like a network fault. Best-effort
    and never fatal: a certificate may be renewed under a running server.
    """
    try:
        decoded = ssl._ssl._test_decode_cert(certfile)  # type: ignore[attr-defined]
        not_after = decoded.get("notAfter")
        not_before = decoded.get("notBefore")
    except Exception:
        return
    now = time.time()
    for label, value, expired in (
        ("expires", not_after, True),
        ("is not valid until", not_before, False),
    ):
        if not value:
            continue
        try:
            stamp = ssl.cert_time_to_seconds(value)
        except ValueError:
            continue
        if (expired and stamp < now) or (not expired and stamp > now):
            _logger.warning(
                "VeloceWorker: the TLS certificate at %s %s %s; clients will fail "
                "the handshake until it is renewed.",
                certfile,
                label,
                value,
            )


def build_ssl_context(ssl_options: dict[str, Any]) -> ssl.SSLContext:
    """Build a server `ssl.SSLContext` from gunicorn's `cfg.ssl_options`.

    A `certfile` is required: `cfg.is_ssl` is true when *either* a cert or key is
    set, but a usable server context needs the chain, so a missing one raises
    `RuntimeError` rather than silently serving cleartext.
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
    # CLIENT_AUTH defaults to CERT_NONE; an explicit cert_reqs (mTLS) wins.
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

    _warn_if_chain_expired(certfile)
    _apply_ssl_version(context, ssl_options.get("ssl_version"))

    ciphers = ssl_options.get("ciphers")
    if ciphers:
        context.set_ciphers(ciphers)

    return context


class VeloceWorker(_GunicornWorker):
    """Gunicorn worker that runs a Veloce app on its own asyncio loop.

    Bridges the master-bound listening socket(s) to an asyncio server whose
    protocol factory yields Veloce's `HttpProtocol`, the same raw HTTP/1.1
    protocol `Veloce.run()` uses. gunicorn owns process supervision; this worker
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
        # Published only once every listener bound, so a partial bind leaks none.
        self._servers: list[asyncio.AbstractServer] = []
        self._stop = asyncio.Event()
        # A changed parent pid means the master died; stop rather than orphan.
        self._initial_ppid: int = getattr(self, "ppid", None) or os.getppid()

    def _request_stop(self) -> None:
        """Wake the serve loop from gunicorn's signal handler thread."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._stop.set)

    def _parent_alive(self) -> bool:
        """Best-effort check that the gunicorn master is still our parent.

        A changed parent pid means the arbiter died and the worker should stop.
        Any failure reading it is treated as "still alive".
        """
        try:
            return os.getppid() == self._initial_ppid
        except OSError:  # pragma: no cover - getppid does not normally fail
            return True

    def _count_request(self) -> None:
        """Increment the handled-request counter and trip max_requests recycling.

        Installed as `HttpProtocol.on_request_complete`. gunicorn's base folds
        `max_requests_jitter` into `self.max_requests` already, so a plain `>=`
        matches its documented behaviour.
        """
        self.nr += 1
        max_requests = getattr(self, "max_requests", 0)
        if max_requests and self.nr >= max_requests:
            self.alive = False
            self._stop.set()

    def _keep_serving(self) -> bool:
        """Report whether a connection may serve its next queued request.

        Installed as `HttpProtocol.should_keep_serving`, so the serve loop stops
        at a request boundary once `max_requests` recycling has cleared `alive`
        instead of draining pipelined requests past the limit.
        """
        return self.alive

    def handle_exit(self, sig: Any, frame: Any) -> None:
        # gunicorn clears self.alive; waking the loop stops us within a tick.
        self._request_stop()
        super().handle_exit(sig, frame)

    def handle_quit(self, sig: Any, frame: Any) -> None:
        self._request_stop()
        super().handle_quit(sig, frame)

    def init_process(self) -> None:
        """Set up a fresh event loop, then hand control to the base class.

        The loop is installed before `super().init_process()` runs the boot
        sequence, so it is current for the whole worker lifetime.
        """
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        # Recaptured post-fork: the liveness check needs this process's parent.
        self._initial_ppid = os.getppid()
        # Process-wide, which is safe: one worker runs per forked process.
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

        gunicorn stores the loaded callable on `self.wsgi`; `self.app` is its own
        Application wrapper, so that is only a fallback.
        """
        return getattr(self, "wsgi", None) or self.app

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """Return a server SSL context when gunicorn was started with TLS.

        `None` when `cfg.is_ssl` is off, which leaves the serving path plain
        HTTP. Otherwise the context built from `cfg.ssl_options` is run through
        gunicorn's documented `ssl_context(config, default_ssl_context_factory)`
        hook, so a deployment customising TLS there is honoured.
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

        Each socket gunicorn bound in the master is handed to
        `loop.create_server`, so the kernel accept queue is shared across
        workers. The loop runs until `self.alive` is cleared.
        """
        app = self._veloce_app()

        await app._run_lifecycle(LIFECYCLE_STARTUP)

        factory = build_protocol_factory(app, loop)
        # Reused rather than rebound, so all workers share one accept queue.
        if not self.sockets:
            raise RuntimeError(
                "VeloceWorker received no listening sockets from gunicorn; "
                "ensure a bind address is configured (e.g. gunicorn --bind)."
            )
        # Without this an HTTPS deployment would silently serve cleartext.
        ssl_context = self._build_ssl_context()

        # One `create_server` per socket; a partial bind closes what it opened,
        # which `run()` would leak on its way to `_shutdown`.
        servers: list[asyncio.AbstractServer] = []
        try:
            for gsock in self.sockets:
                # Re-`listen()`ed, so `backlog` keeps gunicorn's depth.
                servers.append(
                    await loop.create_server(
                        factory,
                        sock=gsock.sock,
                        backlog=_resolve_listen_backlog(),
                        ssl=ssl_context,
                    )
                )
        except BaseException:
            for server in servers:
                server.close()
            for server in servers:
                await server.wait_closed()
            raise

        self._servers = servers

        # notify() within gunicorn's `timeout`, or the master kills us as hung.
        notify_interval = max(1.0, self.timeout / 2.0) if self.timeout else 1.0
        try:
            while self.alive and self._parent_alive():
                self.notify()
                try:
                    # Re-checks liveness even if the signal hook is missed.
                    await asyncio.wait_for(self._stop.wait(), timeout=notify_interval)
                except asyncio.TimeoutError:
                    continue
                break
        finally:
            # Quiesced BEFORE awaiting: since Python 3.12 `wait_closed()` waits
            # out an idle client's 75s keep-alive, past the 30s graceful_timeout
            # after which the master SIGKILLs the worker and this never finishes.
            from veloce.serving.protocol import HttpProtocol

            HttpProtocol.start_graceful_drain()
            for server in self._servers:
                server.close()
            for server in self._servers:
                await server.wait_closed()

    async def _shutdown(self) -> None:
        """Drain in-flight dispatch tasks, then run shutdown lifecycle.

        Mirrors `Veloce._graceful_shutdown`: give active per-request tasks a
        bounded window to finish, cancel stragglers, then run the shutdown hooks.
        """
        from veloce.serving.protocol import HttpProtocol

        app = self._veloce_app()

        HttpProtocol.start_graceful_drain()

        # Hooks STILL installed, or a connection could serve past max_requests.
        if HttpProtocol._active_tasks:
            await asyncio.wait(HttpProtocol._active_tasks, timeout=self.timeout or 30)
        for task in HttpProtocol._active_tasks:
            task.cancel()
        HttpProtocol._active_tasks.clear()

        # Detached now nothing consults them, leaving no process-wide reference.
        if HttpProtocol.on_request_complete == self._count_request:
            HttpProtocol.on_request_complete = None
        if HttpProtocol.should_keep_serving == self._keep_serving:
            HttpProtocol.should_keep_serving = None

        await app._run_lifecycle(LIFECYCLE_SHUTDOWN)
