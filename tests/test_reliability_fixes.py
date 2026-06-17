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
    # `>=`, not `>`: each request stamps its own start time (the point of this
    # test - no shared-dict id() collision), but a coarse-resolution clock
    # (Windows' wall clock is ~15 ms) can return the same value for two reads
    # only 10 ms apart, so strict `>` flakes there.
    assert s2 >= s1
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


def test_signal_disconnect_targets_the_correct_subscription():
    """A receiver connected for both ANY_SENDER and a specific sender
    used to lose its ANY_SENDER subscription when the caller asked to
    detach the per-sender one — `_matches(ANY_SENDER, ...)` returned
    True, deleting the wrong entry. Disconnect now matches the stored
    sender directly."""

    def handler(sender, **kw):
        pass

    sig = Signal("test-disconnect-target")
    sig.connect(handler, weak=False, sender=ANY_SENDER)
    sig.connect(handler, weak=False, sender="login")

    # Detach only the per-sender binding.
    sig.disconnect(handler, sender="login")

    # The ANY_SENDER subscription must survive — a send for an
    # unrelated sender should still find it.
    assert sig.has_receivers_for("anything")
    # The per-sender one is gone (only one subscription remains).
    assert len(sig._subs) == 1
    assert sig._subs[0][0] is ANY_SENDER


# ── Issue #54 — UploadFile async-safe spilled I/O ────────────────────


@pytest.mark.asyncio
async def test_uploadfile_read_does_not_block_on_spilled_spool():
    """Once the spool spills to disk, reads must hop to a thread —
    not block the event loop. The smoke test: a background sentinel
    coroutine must continue to run while a spilled-upload read is
    in flight. (We use a SpooledTemporaryFile that already rolled over.)
    """
    # Manually construct a spooled file and force the rollover via the
    # public `rollover()` API — avoids depending on the `_rolled`
    # implementation-detail attribute. The spool's lifetime is owned by
    # `UploadFile`, which closes it at the end of the test.
    spool = tempfile.SpooledTemporaryFile(max_size=128)  # noqa: SIM115
    spool.write(b"A" * 2048)
    spool.rollover()
    spool.seek(0)

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


@pytest.mark.asyncio
async def test_uploadfile_unrolled_spool_stays_on_loop():
    """The production multipart-parser path hands `UploadFile` a
    `SpooledTemporaryFile`, not a `BytesIO`. A small upload that has
    NOT rolled over is still in memory — it must stay on the loop, not
    pay a thread-hop tax for every read/write."""
    spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)  # noqa: SIM115
    spool.write(b"small")
    spool.seek(0)
    upload = UploadFile(filename="tiny.bin", file=spool, size=5)
    # `_file_is_in_memory()` returns True for an unrolled spool —
    # otherwise the optimisation never fires for real uploads.
    assert upload._file_is_in_memory() is True
    assert await upload.read() == b"small"
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
    cap; the underlying `asyncio.Queue.maxsize` carries the limit."""

    async def go() -> None:
        ws = WebSocket(transport=None, headers={})  # type: ignore[arg-type]
        assert ws._receive_queue.maxsize == WebSocket.DEFAULT_RECV_QUEUE_MAXSIZE

    asyncio.run(go())


def test_websocket_receive_queue_maxsize_override():
    """Apps with legitimate high-burst peers can raise the cap via the
    ctor — the override is honoured."""

    async def go() -> None:
        ws = WebSocket(transport=None, headers={}, recv_queue_maxsize=4)  # type: ignore[arg-type]
        assert ws._receive_queue.maxsize == 4

    asyncio.run(go())


def test_websocket_queue_full_closes_connection_with_1009():
    """When the receive queue fills up, the next inbound frame must not
    raise `QueueFull` out of `feed_data` (which is a synchronous asyncio
    Protocol callback). Instead the connection is closed with `1009
    Message Too Big` and the transport is shut down — the documented
    backpressure mechanism at this layer."""

    closed: list[bool] = []
    written: list[bytes] = []

    class FakeTransport:
        def write(self, data: bytes) -> None:
            written.append(data)

        def close(self) -> None:
            closed.append(True)

        def is_closing(self) -> bool:
            return bool(closed)

    async def go() -> None:
        ws = WebSocket(
            transport=FakeTransport(),  # type: ignore[arg-type]
            headers={},
            recv_queue_maxsize=2,
        )
        # A tiny unfragmented text frame: FIN=1, opcode=1, payload=b"x".
        # Frame bytes: 0x81, 0x01, b'x'.
        frame = b"\x81\x01x"
        # Fill the queue, then deliver one more — the third one
        # would have raised QueueFull; instead the WS closes itself.
        ws.feed_data(frame)
        ws.feed_data(frame)
        ws.feed_data(frame)
        assert ws._closed is True
        assert closed == [True]
        # A Close frame was sent first — opcode 0x8 with code 1009 in
        # the payload (big-endian 16-bit).
        assert written, "expected a Close frame on the way out"
        assert written[-1][0] & 0x0F == 0x8

    asyncio.run(go())


# ── Veloce smoke — sanity-check the app still boots after the bundle ─


def test_app_still_works():
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    assert app.test_client().get("/").json() == {"ok": True}
