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

import pytest

from veloce import Veloce
from veloce.workers import build_protocol_factory, build_ssl_context


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
    from veloce.workers import VeloceWorker

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

    from veloce.workers import VeloceWorker

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


def test_recycling_counter_logic_trips_at_threshold() -> None:
    # Model VeloceWorker._count_request without instantiating the worker (which
    # needs gunicorn): nr increments per request and alive clears at the cap.
    class _Counter:
        def __init__(self, max_requests: int) -> None:
            self.nr = 0
            self.max_requests = max_requests
            self.alive = True

        def count(self) -> None:
            self.nr += 1
            if self.max_requests and self.nr >= self.max_requests:
                self.alive = False

    c = _Counter(max_requests=3)
    c.count()
    c.count()
    assert c.alive is True
    assert c.nr == 2
    c.count()
    assert c.alive is False
    assert c.nr == 3


def test_recycling_disabled_when_max_requests_zero() -> None:
    # max_requests == 0 (or sys.maxsize for the disabled case) must never trip.
    class _Counter:
        def __init__(self) -> None:
            self.nr = 0
            self.max_requests = 0
            self.alive = True

        def count(self) -> None:
            self.nr += 1
            if self.max_requests and self.nr >= self.max_requests:
                self.alive = False

    c = _Counter()
    for _ in range(100):
        c.count()
    assert c.alive is True
    assert c.nr == 100
