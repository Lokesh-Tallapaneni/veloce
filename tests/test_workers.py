"""Tests for the optional gunicorn worker (``veloce.workers``).

gunicorn is an optional, POSIX-only dependency and is not installed in the
default test environment. These tests cover the behaviour that is verifiable
without gunicorn: that importing the package and the worker module both
succeed with gunicorn absent, that instantiating the worker without gunicorn
raises a clear ImportError carrying an install hint, and that the pure-Python
protocol-factory helper works on its own. The end-to-end worker path requires
gunicorn (and a POSIX platform) and is guarded with importorskip.
"""

from __future__ import annotations

import asyncio
import functools
import importlib
import ssl
import sys
import threading

import pytest

from veloce import Veloce
from veloce.workers import VeloceWorker, build_protocol_factory, build_ssl_context


def test_import_veloce_succeeds_without_gunicorn() -> None:
    # Importing the package must never require gunicorn.
    module = importlib.import_module("veloce")
    assert hasattr(module, "Veloce")


def test_import_workers_module_succeeds_without_gunicorn() -> None:
    # Importing the worker module itself must not hard-crash when gunicorn is
    # absent — the base-class import is guarded.
    workers = importlib.import_module("veloce.workers")
    assert hasattr(workers, "VeloceWorker")
    assert hasattr(workers, "build_protocol_factory")


def test_worker_class_is_importable_by_path() -> None:
    # `gunicorn ... -k veloce.workers.VeloceWorker` resolves the class by this
    # exact dotted path; assert it imports and is a class.

    assert isinstance(VeloceWorker, type)


def test_instantiating_without_gunicorn_raises_importerror() -> None:
    # Without gunicorn the worker must refuse to instantiate with a clear,
    # actionable error rather than an obscure failure deeper in gunicorn.
    import veloce.workers as workers

    if workers._GUNICORN_IMPORT_ERROR is None:
        pytest.skip("gunicorn is installed; the no-gunicorn path is not exercised")

    with pytest.raises(ImportError) as excinfo:
        workers.VeloceWorker(0, 0, [], None, 30, None, None)

    message = str(excinfo.value)
    assert "gunicorn" in message
    # The error must point the user at the documented install command.
    assert "pip install veloceframework[gunicorn]" in message


def test_install_hint_names_the_optional_extra() -> None:
    import veloce.workers as workers

    assert "veloceframework[gunicorn]" in workers._INSTALL_HINT
    assert "POSIX" in workers._INSTALL_HINT


def test_build_protocol_factory_returns_bound_callable() -> None:
    # The factory helper is pure Python and unit-testable without gunicorn or
    # a running loop: it binds the app and loop and yields an HttpProtocol.
    from veloce.serving.protocol import HttpProtocol

    app = Veloce()
    loop = asyncio.new_event_loop()
    try:
        factory = build_protocol_factory(app, loop)
        assert isinstance(factory, functools.partial)
        protocol = factory()
        assert isinstance(protocol, HttpProtocol)
        assert protocol.app is app
        assert protocol.loop is loop
    finally:
        loop.close()


def test_worker_subclasses_gunicorn_base_when_available() -> None:
    # Only meaningful when gunicorn is installed: the worker must extend the
    # real gunicorn base class so process supervision plugs in.
    pytest.importorskip("gunicorn")
    from gunicorn.workers.base import Worker

    assert issubclass(VeloceWorker, Worker)


# ── SSL guard (build_ssl_context) ─────────────────────────────────
#
# These exercise the TLS guard that prevents a gunicorn TLS deployment from
# silently serving cleartext. They are pure Python — no gunicorn, no socket,
# no event loop — driven directly off the ssl_options dict gunicorn would
# otherwise hand the worker via cfg.ssl_options.


def test_build_ssl_context_without_certfile_fails_fast() -> None:
    # cfg.is_ssl is true when *either* certfile or keyfile is set, but a server
    # context needs the cert chain. A keyfile-only config must raise rather than
    # let the worker fall through to a cleartext create_server.
    with pytest.raises(RuntimeError) as excinfo:
        build_ssl_context({"keyfile": "/nonexistent/key.pem"})

    message = str(excinfo.value)
    assert "certfile" in message
    assert "cleartext" in message


def test_build_ssl_context_empty_options_fails_fast() -> None:
    # Defensive: an empty options dict (no cert at all) must still fail fast.
    with pytest.raises(RuntimeError):
        build_ssl_context({})


def test_build_ssl_context_unloadable_cert_fails_fast() -> None:
    # A certfile that does not exist (or is malformed) must surface as a
    # RuntimeError, not a partially-built context — again, never a silent
    # cleartext downgrade.
    with pytest.raises(RuntimeError) as excinfo:
        build_ssl_context({"certfile": "/nonexistent/cert.pem"})

    assert "TLS cert chain" in str(excinfo.value)


def _write_self_signed_cert(tmp_path):
    # Generate a throwaway self-signed cert/key pair for the success path.
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


def test_build_ssl_context_loads_valid_cert_chain(tmp_path) -> None:
    pytest.importorskip("cryptography")
    certfile, keyfile = _write_self_signed_cert(tmp_path)

    context = build_ssl_context({"certfile": certfile, "keyfile": keyfile})

    assert isinstance(context, ssl.SSLContext)
    # A server context that does not require client certs (the default).
    assert context.verify_mode == ssl.CERT_NONE


def test_build_ssl_context_honours_explicit_cert_reqs(tmp_path) -> None:
    pytest.importorskip("cryptography")
    certfile, keyfile = _write_self_signed_cert(tmp_path)

    context = build_ssl_context(
        {
            "certfile": certfile,
            "keyfile": keyfile,
            "cert_reqs": ssl.CERT_REQUIRED,
            "ca_certs": certfile,
        }
    )

    assert context.verify_mode == ssl.CERT_REQUIRED


# ── max_requests recycling hook ───────────────────────────────────


def test_protocol_request_complete_hook_defaults_none() -> None:
    # The per-request hook the worker uses for max_requests recycling must be
    # absent by default so the uvicorn / Veloce.run() path pays nothing for it.
    from veloce.serving.protocol import HttpProtocol

    assert HttpProtocol.on_request_complete is None


class _RecyclingStub:
    """The state `VeloceWorker._count_request` touches, without gunicorn.

    The method is called unbound against this. Modelling the *logic* in the test
    instead - which is what these tests used to do - meant the shipped method was
    never executed, so `self._stop.set()` was not covered at all and the two
    copies could disagree without anything failing.
    """

    def __init__(self, max_requests: int) -> None:
        self.nr = 0
        self.max_requests = max_requests
        self.alive = True
        self._stop = threading.Event()

    def count(self) -> None:
        VeloceWorker._count_request(self)


def test_recycling_counter_trips_at_threshold() -> None:
    worker = _RecyclingStub(max_requests=3)
    worker.count()
    worker.count()
    assert worker.alive is True
    assert worker.nr == 2

    worker.count()
    assert worker.alive is False
    assert worker.nr == 3


def test_tripping_the_threshold_wakes_the_heartbeat_loop() -> None:
    """`_stop` is set so the master can replace the worker promptly.

    The re-implemented counter these tests used omitted this line entirely.
    """
    worker = _RecyclingStub(max_requests=1)
    assert not worker._stop.is_set()
    worker.count()
    assert worker._stop.is_set()


def test_the_counter_keeps_counting_past_the_threshold() -> None:
    """A request in flight when the cap trips must still be counted."""
    worker = _RecyclingStub(max_requests=2)
    for _ in range(5):
        worker.count()
    assert worker.nr == 5
    assert worker.alive is False


def test_recycling_disabled_when_max_requests_zero() -> None:
    worker = _RecyclingStub(max_requests=0)
    for _ in range(100):
        worker.count()
    assert worker.alive is True
    assert worker.nr == 100
    assert not worker._stop.is_set()


def test_recycling_disabled_when_max_requests_is_maxsize() -> None:
    """gunicorn uses `sys.maxsize` for the disabled case, not `0`."""
    worker = _RecyclingStub(max_requests=sys.maxsize)
    for _ in range(100):
        worker.count()
    assert worker.alive is True
    assert not worker._stop.is_set()


def test_a_missing_max_requests_attribute_does_not_recycle() -> None:
    """The shipped method reads it with a default; a stub without it must not
    trip - which the modelled copy could not have shown."""

    class _Bare:
        def __init__(self) -> None:
            self.nr = 0
            self.alive = True
            self._stop = threading.Event()

    bare = _Bare()
    for _ in range(10):
        VeloceWorker._count_request(bare)
    assert bare.alive is True
    assert bare.nr == 10


def test_protocol_keep_serving_hook_defaults_none() -> None:
    # The serve-loop predicate the worker uses to honour max_requests at the
    # request boundary must be absent by default so non-gunicorn paths pay
    # nothing for it.
    from veloce.serving.protocol import HttpProtocol

    assert HttpProtocol.should_keep_serving is None


def test_keep_serving_reports_alive_flag() -> None:
    # VeloceWorker._keep_serving returns self.alive so the per-connection serve
    # loop stops draining queued/pipelined requests once recycling clears alive.
    # Exercised without gunicorn by binding the unbound method to a stub.

    class _Stub:
        alive = True

    stub = _Stub()
    assert VeloceWorker._keep_serving(stub) is True
    stub.alive = False
    assert VeloceWorker._keep_serving(stub) is False


# ── TLS customization hook (cfg.ssl_context) ──────────────────────
#
# gunicorn's documented TLS customization point is the ssl_context(config,
# default_ssl_context_factory) hook; its own socket layer calls
# conf.ssl_context(conf, default_ssl_context_factory). _build_ssl_context must
# route the default context through that hook so configured customizations
# (minimum TLS version, mTLS tweaks) are honoured. Driven without gunicorn by
# binding the unbound method to a config stub.


# ── multi-bind partial-failure listener cleanup ───────────────────
#
# _serve creates one asyncio server per bound socket. If a later bind fails,
# the servers already created must be closed before the error propagates —
# otherwise a live listener survives into _shutdown() (run() proceeds straight
# to _shutdown on failure) and leaks. Driven without gunicorn by binding the
# unbound _serve to a stub and feeding a loop whose create_server fails midway.


class _FakeServer:
    def __init__(self) -> None:
        self.closed = False
        self.waited = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


class _FakeGSock:
    def __init__(self, sock) -> None:
        self.sock = sock


class _ServeStub:
    """Minimal stand-in exposing only what _serve touches."""

    def __init__(self, sockets, fail_after: int) -> None:
        self.sockets = sockets
        self._server = None
        self.timeout = 30
        self.alive = True
        self.created: list[_FakeServer] = []
        self._fail_after = fail_after
        self._app = Veloce()

    def _veloce_app(self) -> Veloce:
        return self._app

    def _build_ssl_context(self):
        return None


async def test_serve_closes_partial_listeners_when_a_later_bind_fails() -> None:

    sockets = [_FakeGSock(object()), _FakeGSock(object()), _FakeGSock(object())]
    stub = _ServeStub(sockets, fail_after=1)

    class _Loop:
        async def create_server(self, factory, sock, backlog, ssl):
            # `backlog` is positionally required here on purpose: asyncio
            # re-`listen()`s the socket gunicorn already bound, so omitting it
            # silently replaced the configured depth with asyncio's default.
            assert backlog >= 512, backlog
            if len(stub.created) >= stub._fail_after:
                raise OSError("address already in use")
            server = _FakeServer()
            stub.created.append(server)
            return server

    with pytest.raises(OSError):
        await VeloceWorker._serve(stub, _Loop())

    # The one listener that bound before the failure must be closed and awaited,
    # and never published as self._server (which would survive into _shutdown).
    assert len(stub.created) == 1
    assert stub.created[0].closed is True
    assert stub.created[0].waited is True
    assert stub._server is None


class _CfgStub:
    def __init__(self, *, is_ssl, ssl_options, ssl_context=None) -> None:
        self.is_ssl = is_ssl
        self.ssl_options = ssl_options
        self.ssl_context = ssl_context


class _WorkerStub:
    def __init__(self, cfg) -> None:
        self.cfg = cfg


def test_build_ssl_context_returns_none_when_tls_off() -> None:

    worker = _WorkerStub(_CfgStub(is_ssl=False, ssl_options={}))
    assert VeloceWorker._build_ssl_context(worker) is None


def test_build_ssl_context_uses_default_factory_without_hook(tmp_path) -> None:
    pytest.importorskip("cryptography")

    certfile, keyfile = _write_self_signed_cert(tmp_path)
    cfg = _CfgStub(
        is_ssl=True,
        ssl_options={"certfile": certfile, "keyfile": keyfile},
        ssl_context=None,
    )
    worker = _WorkerStub(cfg)

    context = VeloceWorker._build_ssl_context(worker)
    assert isinstance(context, ssl.SSLContext)


def test_build_ssl_context_invokes_customization_hook(tmp_path) -> None:
    pytest.importorskip("cryptography")

    certfile, keyfile = _write_self_signed_cert(tmp_path)
    seen = {}

    def hook(config, default_ssl_context_factory):
        seen["config"] = config
        context = default_ssl_context_factory()
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        return context

    cfg = _CfgStub(
        is_ssl=True,
        ssl_options={"certfile": certfile, "keyfile": keyfile},
        ssl_context=hook,
    )
    worker = _WorkerStub(cfg)

    context = VeloceWorker._build_ssl_context(worker)
    assert isinstance(context, ssl.SSLContext)
    # The configured customization must be honoured, not discarded.
    assert context.minimum_version == ssl.TLSVersion.TLSv1_3
    # gunicorn passes the config object through as the hook's first argument.
    assert seen["config"] is cfg


def test_build_ssl_context_rejects_non_context_from_hook(tmp_path) -> None:
    pytest.importorskip("cryptography")

    certfile, keyfile = _write_self_signed_cert(tmp_path)

    def bad_hook(config, default_ssl_context_factory):
        return "not a context"

    cfg = _CfgStub(
        is_ssl=True,
        ssl_options={"certfile": certfile, "keyfile": keyfile},
        ssl_context=bad_hook,
    )
    worker = _WorkerStub(cfg)

    with pytest.raises(RuntimeError) as excinfo:
        VeloceWorker._build_ssl_context(worker)
    assert "ssl_context hook" in str(excinfo.value)


def test_build_ssl_context_hook_still_fails_fast_on_missing_cert() -> None:
    # The hook receives a factory; if it calls it with no certfile configured,
    # the fail-fast guard inside build_ssl_context still fires (no cleartext).

    def hook(config, default_ssl_context_factory):
        return default_ssl_context_factory()

    cfg = _CfgStub(is_ssl=True, ssl_options={"keyfile": "/nonexistent/key.pem"}, ssl_context=hook)
    worker = _WorkerStub(cfg)

    with pytest.raises(RuntimeError) as excinfo:
        VeloceWorker._build_ssl_context(worker)
    assert "certfile" in str(excinfo.value)
