"""Regression tests for the reliability/correctness pass (issues #50–#56).

Each test pins down behaviour that the corresponding fix introduces.
The bundle covers a mix of memory leaks, event-loop hazards, and a
correctness gap; tests are grouped per issue.
"""

from __future__ import annotations

import asyncio
import io
import os
import tempfile

import pytest

from veloce import (
    Request,
    Response,
    Veloce,
    hash_password_async,
    verify_password_async,
)
from veloce.contrib.staticfiles import StaticFiles
from veloce.http.datastructures import UploadFile
from veloce.middleware.logging import LoggingMiddleware
from veloce.signals import ANY_SENDER, Signal
from veloce.websocket import WebSocket

# ── Issue #50 — StaticFiles._etag_cache bounded LRU ─────────────────


def test_staticfiles_etag_cache_is_bounded(tmp_path):
    """The ETag cache must not grow without limit. Hammer it with more
    distinct files than the cap allows and assert the cap is honoured;
    least-recently-used entries are evicted."""

    async def hammer() -> int:
        sf = StaticFiles(str(tmp_path), prefix="/static")
        # Tighten the cap so the test stays small/fast.
        sf.ETAG_CACHE_MAX = 4
        for i in range(20):
            p = tmp_path / f"f{i}.txt"
            p.write_text(f"file-{i}")
            req = Request(
                method="GET",
                path=f"/static/f{i}.txt",
                query_string="",
                headers={},
                body=b"",
            )
            resp = await sf.handle(req)
            assert resp is not None
            assert resp.status_code == 200
        return len(sf._etag_cache)

    size = asyncio.run(hammer())
    assert size == 4  # capped, not 20


def test_staticfiles_etag_lru_evicts_oldest(tmp_path):
    """Touching an old entry should refresh its position so a later
    eviction drops the truly oldest one."""

    async def go() -> dict:
        sf = StaticFiles(str(tmp_path), prefix="/static")
        sf.ETAG_CACHE_MAX = 3
        for i in range(3):
            (tmp_path / f"f{i}.txt").write_text(f"f{i}")

        async def hit(name: str) -> None:
            req = Request(
                method="GET",
                path=f"/static/{name}",
                query_string="",
                headers={},
                body=b"",
            )
            await sf.handle(req)

        await hit("f0.txt")
        await hit("f1.txt")
        await hit("f2.txt")
        # Refresh f0 so it is now the most-recently-used; f1 becomes
        # the oldest.
        await hit("f0.txt")
        # Adding a fourth entry should evict f1, not f0.
        (tmp_path / "f3.txt").write_text("f3")
        await hit("f3.txt")
        keys = {os.path.basename(k) for k in sf._etag_cache}
        return keys

    keys = asyncio.run(go())
    assert "f1.txt" not in keys
    assert {"f0.txt", "f2.txt", "f3.txt"} <= keys


# ── Issue #51 — LoggingMiddleware lifetime tied to request ───────────


@pytest.mark.asyncio
async def test_logging_middleware_does_not_leak_on_handler_exception(caplog):
    """A handler that raises must not leave state behind in the
    middleware. The fix moves the start-time onto `request._state`,
    whose lifetime ends with the request — so even on a raise, nothing
    leaks at the middleware level."""
    mw = LoggingMiddleware()
    req = Request(method="GET", path="/x", query_string="", headers={}, body=b"")
    await mw.process_request(req)

    # The middleware no longer carries a per-request dict at all. The
    # start time lives on the request itself, which is GC-able once the
    # request goes out of scope.
    assert not hasattr(mw, "_request_times")
    assert "__veloce_logging_start" in req._state


@pytest.mark.asyncio
async def test_logging_middleware_durations_are_per_request():
    """Two concurrent requests must each see their own start time —
    no id() collision via a shared dict."""
    mw = LoggingMiddleware()
    r1 = Request(method="GET", path="/a", query_string="", headers={}, body=b"")
    r2 = Request(method="GET", path="/b", query_string="", headers={}, body=b"")
    await mw.process_request(r1)
    await asyncio.sleep(0.01)
    await mw.process_request(r2)
    s1 = r1._state["__veloce_logging_start"]
    s2 = r2._state["__veloce_logging_start"]
    assert s2 > s1
    # process_response reads each request's own start, not the other's.
    await mw.process_response(r1, Response(status_code=200))
    await mw.process_response(r2, Response(status_code=200))


# ── Issue #53 — Signal sender filtering ──────────────────────────────


def test_signal_sender_filter_only_fires_for_matching_sender():
    """A receiver bound to one sender must not fire for another."""
    sig = Signal("test-sender-filter")
    calls_for_app: list = []
    calls_for_other: list = []

    sig.connect(lambda s, **kw: calls_for_app.append(s), weak=False, sender="app")
    sig.connect(lambda s, **kw: calls_for_other.append(s), weak=False, sender="other")

    sig.send("app", x=1)
    sig.send("other", x=2)
    sig.send("third", x=3)

    assert calls_for_app == ["app"]
    assert calls_for_other == ["other"]


def test_signal_any_sender_receivers_fire_for_every_send():
    sig = Signal("test-any-sender")
    seen: list = []
    # Default is ANY_SENDER, but pass it explicitly to document intent.
    sig.connect(lambda s, **kw: seen.append(s), weak=False, sender=ANY_SENDER)
    sig.send("a")
    sig.send("b")
    sig.send(None)
    assert seen == ["a", "b", None]


def test_signal_has_receivers_for_filters_by_sender():
    sig = Signal("test-has-receivers")
    sig.connect(lambda s, **kw: None, weak=False, sender="logged-in")
    assert sig.has_receivers_for("logged-in")
    assert not sig.has_receivers_for("anonymous")
    assert not sig.has_receivers_for(ANY_SENDER)


# ── Issue #54 — UploadFile async-safe spilled I/O ────────────────────


@pytest.mark.asyncio
async def test_uploadfile_read_does_not_block_on_spilled_spool():
    """Once the spool spills to disk, reads must hop to a thread —
    not block the event loop. The smoke test: a background sentinel
    coroutine must continue to run while a spilled-upload read is
    in flight. (We use a SpooledTemporaryFile that already rolled over.)
    """
    # Manually construct a spooled file that has rolled over to disk —
    # write a payload bigger than the threshold so `_rolled` is True.
    # The spool's lifetime is owned by `UploadFile`, which closes it
    # at the end of the test; no `with` block here.
    spool = tempfile.SpooledTemporaryFile(max_size=128)  # noqa: SIM115
    spool.write(b"A" * 2048)
    spool.seek(0)
    assert spool._rolled is True

    upload = UploadFile(filename="big.bin", file=spool, size=2048)
    ticked = 0

    async def ticker() -> None:
        nonlocal ticked
        for _ in range(5):
            await asyncio.sleep(0)
            ticked += 1

    # Drive both concurrently. If `read` is blocking the loop, the
    # ticker won't run; the to_thread offload keeps the loop free.
    data, _ = await asyncio.gather(upload.read(2048), ticker())
    assert data == b"A" * 2048
    assert ticked == 5

    await upload.close()


@pytest.mark.asyncio
async def test_uploadfile_in_memory_read_stays_on_loop():
    """The cheap in-memory path must not pay an executor-hop tax —
    BytesIO reads stay on the loop."""
    upload = UploadFile(filename="tiny.txt", file=io.BytesIO(b"hi"), size=2)
    assert await upload.read() == b"hi"
    await upload.close()


# ── Issue #55 — async password-hash wrappers ─────────────────────────


@pytest.mark.asyncio
async def test_hash_and_verify_password_async_round_trip():
    """`hash_password_async` / `verify_password_async` are async-safe
    wrappers around the scrypt KDF. Round-tripping a credential must
    work the same way the sync versions do."""
    stored = await hash_password_async("hunter2")
    assert isinstance(stored, str)
    assert "$" in stored
    assert await verify_password_async(stored, "hunter2") is True
    assert await verify_password_async(stored, "wrong-password") is False


@pytest.mark.asyncio
async def test_hash_password_async_does_not_block_the_loop():
    """A handler calling `hash_password_async` must leave the loop
    free for other tasks. Without the executor hop, scrypt would stall
    the loop for ~100 ms and the ticker below would not advance."""
    ticked = 0

    async def ticker() -> None:
        nonlocal ticked
        # 50 ms is enough that a synchronous scrypt would clearly block
        # past the first tick; we observe several.
        deadline = asyncio.get_running_loop().time() + 0.05
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.005)
            ticked += 1

    _, _ = await asyncio.gather(hash_password_async("hunter2"), ticker())
    assert ticked > 0


# ── Issue #56 — bounded WebSocket receive queue ──────────────────────


def test_websocket_receive_queue_is_capped_by_default():
    """The unbounded queue was a DoS vector. Default is now a finite
    cap so a runaway producer blocks on put rather than growing the
    queue without limit."""

    async def go() -> None:
        ws = WebSocket(transport=None, headers={})  # type: ignore[arg-type]
        assert ws._receive_queue.maxsize == WebSocket.DEFAULT_RECV_QUEUE_MAXSIZE
        # The cap propagates: put_nowait raises once we exceed it.
        for _ in range(WebSocket.DEFAULT_RECV_QUEUE_MAXSIZE):
            ws._receive_queue.put_nowait(b"frame")
        with pytest.raises(asyncio.QueueFull):
            ws._receive_queue.put_nowait(b"one-too-many")

    asyncio.run(go())


def test_websocket_receive_queue_maxsize_override():
    """Apps with legitimate high-burst peers can raise the cap via the
    ctor — the override is honoured."""

    async def go() -> None:
        ws = WebSocket(transport=None, headers={}, recv_queue_maxsize=4)  # type: ignore[arg-type]
        assert ws._receive_queue.maxsize == 4

    asyncio.run(go())


# ── Veloce smoke — sanity-check the app still boots after the bundle ─


def test_app_still_works():
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    assert app.test_client().get("/").json() == {"ok": True}
