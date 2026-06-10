"""The native dev server installs a cooperative shutdown handler everywhere.

On POSIX `loop.add_signal_handler` is used. On Windows it raises
`NotImplementedError`, so `_serve` falls back to `signal.signal` and bounces the
cooperative shutdown (`server.close()` + set the shutdown event) onto the loop —
otherwise Ctrl+C / Ctrl+Break would raise `KeyboardInterrupt` straight out of the
loop and drop in-flight connections before the graceful drain runs.
"""

from __future__ import annotations

import asyncio
import signal

from veloce import Veloce


class _FakeServer:
    """`loop.create_server` stand-in that records `close()`."""

    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self) -> _FakeServer:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


async def test_signal_fallback_when_add_signal_handler_unsupported(monkeypatch):
    app = Veloce(openapi_url=None)
    loop = asyncio.get_running_loop()

    server = _FakeServer()

    async def fake_create_server(*args: object, **kwargs: object) -> _FakeServer:
        return server

    monkeypatch.setattr(loop, "create_server", fake_create_server)

    # Simulate Windows: the loop cannot install signal handlers.
    def _raise(*args: object, **kwargs: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", _raise)

    # Capture the signal.signal fallback registrations.
    installed: dict[int, object] = {}

    def fake_signal(sig: int, handler: object) -> object:
        installed[sig] = handler
        return None

    monkeypatch.setattr(signal, "signal", fake_signal)

    task = asyncio.create_task(app._serve("127.0.0.1", 0, False, None))
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if signal.SIGINT in installed:
                break
        # The Windows fallback wired a SIGINT handler via signal.signal.
        assert signal.SIGINT in installed

        # Invoking it (as the OS would on Ctrl+C) schedules the cooperative
        # shutdown on the loop; `_serve` then drains and returns.
        installed[signal.SIGINT](signal.SIGINT, None)  # type: ignore[operator]
        await asyncio.wait_for(task, timeout=2.0)
        assert server.closed
    finally:
        if not task.done():
            task.cancel()


async def test_signal_fallback_restores_previous_handler(monkeypatch):
    app = Veloce(openapi_url=None)
    loop = asyncio.get_running_loop()
    server = _FakeServer()

    async def fake_create_server(*args: object, **kwargs: object) -> _FakeServer:
        return server

    monkeypatch.setattr(loop, "create_server", fake_create_server)

    def _raise(*args: object, **kwargs: object) -> None:
        raise NotImplementedError

    monkeypatch.setattr(loop, "add_signal_handler", _raise)

    # The native server saves each previous handler and restores it after serving,
    # so it does not leak a loop-closured handler past `run()`.
    monkeypatch.setattr(signal, "getsignal", lambda sig: "ORIG")
    calls: list[tuple[int, object]] = []

    def fake_signal(sig: int, handler: object) -> object:
        calls.append((sig, handler))
        return None

    monkeypatch.setattr(signal, "signal", fake_signal)

    task = asyncio.create_task(app._serve("127.0.0.1", 0, False, None))
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            if any(s == signal.SIGINT for s, _ in calls):
                break
        handler = next(h for s, h in calls if s == signal.SIGINT)
        handler(signal.SIGINT, None)  # type: ignore[operator]
        await asyncio.wait_for(task, timeout=2.0)
        # The last signal.signal call for SIGINT restored the saved handler.
        sigint_handlers = [h for s, h in calls if s == signal.SIGINT]
        assert sigint_handlers[-1] == "ORIG"
    finally:
        if not task.done():
            task.cancel()
