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

import pytest

from veloce import Veloce
from veloce.workers import build_protocol_factory


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
