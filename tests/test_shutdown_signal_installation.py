"""Installing the shutdown signals is one decision, made in one place.

`_serve` was 100 lines of which roughly 60 were the cross-platform signal
question - which handler mechanism the platform supports, the Windows polling
fallback, and restoring process-wide handlers afterwards - wrapped around about
fifteen lines of actual serving.

The two return values are the whole contract: whether the loop owns the
handlers, which decides how `_serve` waits, and what has to be put back.
"""

from __future__ import annotations

import asyncio
import signal

import pytest

from veloce import Veloce
from veloce.app.serving import ServingMixin


def _install(loop, on_shutdown):
    return ServingMixin._install_shutdown_signals(loop, on_shutdown)


async def test_the_loop_path_reports_ownership_and_nothing_to_restore(monkeypatch) -> None:
    """POSIX: the loop installs the handler and runs it on the loop thread."""
    loop = asyncio.get_running_loop()
    installed: list[int] = []
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, cb: installed.append(sig), raising=False
    )

    native, restore = _install(loop, lambda: None)
    assert native is True
    assert restore == []
    assert set(installed) == {signal.SIGINT, signal.SIGTERM}


async def test_the_fallback_path_reports_what_it_must_put_back(monkeypatch) -> None:
    """Windows: `signal.signal` is process-wide, so the previous handler returns."""
    loop = asyncio.get_running_loop()

    def _unsupported(sig, cb):
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", _unsupported, raising=False)

    replaced: dict[int, object] = {}
    monkeypatch.setattr(signal, "getsignal", lambda sig: f"previous-{sig}")
    monkeypatch.setattr(signal, "signal", lambda sig, cb: replaced.setdefault(sig, cb))

    native, restore = _install(loop, lambda: None)
    assert native is False
    assert restore, "nothing to restore means a process-wide handler leaks past run()"
    assert all(previous == f"previous-{sig}" for sig, previous in restore)
    assert signal.SIGINT in dict(restore)


async def test_a_signal_that_cannot_be_installed_is_skipped(monkeypatch) -> None:
    """`signal.signal` only works on the main thread; a worker thread must not die."""
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda *a: (_ for _ in ()).throw(NotImplementedError)
    )
    monkeypatch.setattr(signal, "getsignal", lambda sig: None)

    def _refuses(sig, cb):
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(signal, "signal", _refuses)

    native, restore = _install(loop, lambda: None)
    assert native is False
    assert restore == []


async def test_the_fallback_handler_bounces_onto_the_loop(monkeypatch) -> None:
    """The point of the fallback: shutdown runs on the loop, not the signal frame."""
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda *a: (_ for _ in ()).throw(NotImplementedError)
    )
    monkeypatch.setattr(signal, "getsignal", lambda sig: None)
    handlers: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, cb: handlers.setdefault(sig, cb))

    fired = asyncio.Event()
    _install(loop, fired.set)

    handlers[signal.SIGINT](signal.SIGINT, None)
    await asyncio.wait_for(fired.wait(), 1)


async def test_a_late_event_on_a_closed_loop_is_ignored(monkeypatch) -> None:
    """A console control event can arrive once the loop is already closing."""
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda *a: (_ for _ in ()).throw(NotImplementedError)
    )
    monkeypatch.setattr(signal, "getsignal", lambda sig: None)
    handlers: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, cb: handlers.setdefault(sig, cb))
    _install(loop, lambda: pytest.fail("shutdown ran on a closed loop"))

    monkeypatch.setattr(loop, "is_closed", lambda: True)
    handlers[signal.SIGINT](signal.SIGINT, None)  # must not raise


def test_serve_no_longer_carries_the_platform_question() -> None:
    """The split, checked at the source: `_serve` reads as serving."""
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "src" / "veloce" / "app" / "serving.py"
    ).read_text(encoding="utf-8")
    cls = next(n for n in ast.parse(source).body if isinstance(n, ast.ClassDef))
    serve = next(
        f
        for f in cls.body
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name == "_serve"
    )
    body = ast.unparse(serve)
    assert "add_signal_handler" not in body
    assert "signal.signal(" not in body
    assert "_install_shutdown_signals" in body
    assert serve.end_lineno - serve.lineno + 1 < 70, "_serve is growing back"


def test_serving_still_works_end_to_end() -> None:
    """The behaviour, not just the shape - through the public test client."""
    from veloce.testclient import TestClient

    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/").json() == {"ok": True}


def test_restoring_puts_back_exactly_what_was_saved(monkeypatch) -> None:
    restored: list[tuple[int, object]] = []
    monkeypatch.setattr(signal, "signal", lambda sig, cb: restored.append((sig, cb)))
    ServingMixin._restore_shutdown_signals([(signal.SIGINT, "previous")])
    assert restored == [(signal.SIGINT, "previous")]


def test_restoring_nothing_is_a_no_op(monkeypatch) -> None:
    monkeypatch.setattr(signal, "signal", lambda sig, cb: pytest.fail("nothing to restore"))
    ServingMixin._restore_shutdown_signals([])


def test_a_failed_restore_does_not_become_the_reason_run_raises(monkeypatch) -> None:
    """Restoration runs while the process is already shutting down."""

    def _refuses(sig, cb):
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(signal, "signal", _refuses)
    ServingMixin._restore_shutdown_signals([(signal.SIGINT, None)])
