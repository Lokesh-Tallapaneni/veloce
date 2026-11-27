"""Which event loop the sync test client drives the app on.

The in-memory client opens no socket and spawns no process. On Windows the
default proactor loop puts an I/O completion port in the way of every loop
iteration regardless, so the client builds a selector loop instead.

The one thing a selector loop cannot do on Windows is `create_subprocess_*`,
so a caller may pass their own loop. That escape hatch is the reason the switch
is safe, and it is pinned here.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct
import subprocess
import sys

import pytest

from veloce import Veloce
from veloce.testclient import TestClient, _abort_self_pipe_on_close, _new_loop


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index() -> dict:
        return {"ok": True}

    return app


# ── The loop the client picks ────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="the proactor default is Windows-only")
def test_a_windows_client_drives_a_selector_loop():
    loop = _new_loop()
    try:
        assert isinstance(loop, asyncio.SelectorEventLoop)
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows default")
def test_elsewhere_the_platform_default_is_kept():
    loop = _new_loop()
    try:
        assert not loop.is_closed()
    finally:
        loop.close()


def test_the_client_uses_the_loop_the_factory_built():
    client = TestClient(_app())
    try:
        assert client._loop is not None
        assert client._owns_loop is True
        if sys.platform == "win32":
            assert isinstance(client._loop, asyncio.SelectorEventLoop)
    finally:
        client.close()


def test_the_client_still_serves_requests_on_it():
    """The whole point: the cheaper loop must run the app identically."""
    client = TestClient(_app())
    try:
        assert client.get("/").json() == {"ok": True}
    finally:
        client.close()


# ── The escape hatch ─────────────────────────────────────────────────


def test_a_supplied_loop_is_used():
    loop = asyncio.new_event_loop()
    try:
        client = TestClient(_app(), loop=loop)
        assert client._loop is loop
        assert client.get("/").json() == {"ok": True}
    finally:
        loop.close()


def test_a_supplied_loop_is_not_closed_by_the_client():
    """It is the caller's loop; closing it would break their next client."""
    loop = asyncio.new_event_loop()
    try:
        client = TestClient(_app(), loop=loop)
        client.get("/")
        client.close()
        assert loop.is_closed() is False
        # And it is still usable afterwards.
        assert TestClient(_app(), loop=loop).get("/").status_code == 200
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the proactor loop is Windows-only")
def test_a_proactor_loop_can_be_supplied_for_a_handler_that_spawns_a_subprocess():
    """The documented recourse for the one thing a selector loop cannot do."""
    loop = asyncio.ProactorEventLoop()
    try:
        app = Veloce(openapi_url=None)

        @app.get("/spawn")
        async def spawn() -> dict:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "print('hi')",
                stdout=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            return {"out": out.decode().strip()}

        client = TestClient(app, loop=loop)
        assert client.get("/spawn").json() == {"out": "hi"}
    finally:
        loop.close()


# ── What the selector loop costs, and does not ───────────────────────


def _ports_in_time_wait() -> set[int]:
    out = subprocess.run(["netstat", "-an"], capture_output=True, text=True).stdout
    ports = set()
    for line in out.splitlines():
        if "TIME_WAIT" in line:
            parts = line.split()
            if len(parts) > 1 and ":" in parts[1]:
                with contextlib.suppress(ValueError):
                    ports.add(int(parts[1].rsplit(":", 1)[1]))
    return ports


def _self_pipe_ports(loop: asyncio.AbstractEventLoop) -> set[int]:
    return {
        getattr(loop, name).getsockname()[1]
        for name in ("_ssock", "_csock")
        if getattr(loop, name, None) is not None
    }


@pytest.mark.skipif(sys.platform != "win32", reason="the self-pipe is a socketpair on Windows only")
def test_the_self_pipe_is_set_to_close_abortively():
    loop = _new_loop()
    try:
        for name in ("_ssock", "_csock"):
            # Windows reports `linger` as two `u_short`, four bytes in all.
            raw = getattr(loop, name).getsockopt(socket.SOL_SOCKET, socket.SO_LINGER, 16)
            onoff, linger = struct.unpack("HH", raw[:4])
            assert onoff == 1
            assert linger == 0
    finally:
        loop.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the self-pipe is a socketpair on Windows only")
def test_the_self_pipe_releases_its_ports_on_close():
    """The regression this exists for: one held port per client exhausted the
    16384-port dynamic range mid-suite, after which clients failed to construct.

    Asserted on the pipe's OWN port numbers rather than a global `TIME_WAIT`
    count, so another process on the machine cannot move the result - and it
    costs two ports instead of one per measured client, which a count-based
    version of this test would itself leak.
    """
    fixed = _new_loop()
    fixed_ports = _self_pipe_ports(fixed)
    fixed.close()

    unfixed = asyncio.SelectorEventLoop()
    unfixed_ports = _self_pipe_ports(unfixed)
    unfixed.close()

    held = _ports_in_time_wait()
    assert unfixed_ports & held, "the comparison arm must show the leak this test is about"
    assert not (fixed_ports & held)


def test_a_loop_without_a_self_pipe_is_left_alone():
    """`_ssock` / `_csock` are CPython internals; a rename must not raise."""
    _abort_self_pipe_on_close(object())  # type: ignore[arg-type]
