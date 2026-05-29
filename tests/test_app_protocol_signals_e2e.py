"""End-to-end regression tests for app / protocol / signals fixes."""

from __future__ import annotations

import asyncio
import logging

import pytest

from veloce import Veloce
from veloce.background import BackgroundTask
from veloce.http.response import Response
from veloce.serving.protocol import (
    MAX_HEADER_SIZE,
    MAX_TOTAL_HEADERS_SIZE,
    MAX_URL_SIZE,
    HttpProtocol,
)
from veloce.signals import Signal

# ── Fake transport used by protocol-level tests ─────────────────────


class _FakeTransport(asyncio.Transport):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed


# ── 1. bind_all + explicit host conflict ────────────────────────────


def test_run_bind_all_conflicts_with_explicit_host():
    app = Veloce(openapi_url=None)
    with pytest.raises(ValueError, match="bind_all"):
        app.run(host="192.168.1.10", bind_all=True)


# ── 2. app.routes cache identity + invalidation on add_url_rule ─────


def test_routes_cache_returns_same_object_until_mutation():
    app = Veloce(openapi_url=None)

    async def first(request):
        return {"ok": True}

    app.add_url_rule("/first", endpoint="first", view_func=first)
    snap1 = app.routes
    snap2 = app.routes
    assert snap1 is snap2, "cache hit should return the same list object"

    async def second(request):
        return {"ok": True}

    app.add_url_rule("/second", endpoint="second", view_func=second)
    snap3 = app.routes
    assert snap3 is not snap1, "add_url_rule must invalidate the routes cache"
    paths = {r["path"] for r in snap3}
    assert "/first" in paths and "/second" in paths


# ── 3. view_functions snapshot — caller mutation must not poison ────


def test_view_functions_returns_fresh_snapshot():
    app = Veloce(openapi_url=None)

    async def index(request):
        return "hi"

    app.add_url_rule("/", endpoint="index", view_func=index)

    vf1 = app.view_functions
    assert "index" in vf1
    vf1["index"] = "poisoned"  # caller mutation
    vf1["junk"] = lambda: None

    vf2 = app.view_functions
    assert vf2["index"] is index, "framework state must not be poisoned by caller mutation"
    assert "junk" not in vf2


# ── 4. Background task failure is logged via app.logger ─────────────


def test_attached_background_task_failure_logs_on_app_logger(caplog):
    app = Veloce(openapi_url=None)

    def boom():
        raise RuntimeError("background-task-boom")

    @app.get("/bg")
    async def view(request):
        return Response(
            body=b"ok",
            content_type="text/plain",
            background=BackgroundTask(boom),
        )

    client = app.test_client()
    with caplog.at_level(logging.ERROR, logger=app.logger.name):
        resp = client.get("/bg")
        assert resp.status_code == 200
        # Drain the loop so the BackgroundTask + done-callback have a chance to run.
        loop = getattr(client, "_loop", None) or asyncio.new_event_loop()
        for _ in range(5):
            loop.run_until_complete(asyncio.sleep(0))

    messages = [r.getMessage() for r in caplog.records if r.name == app.logger.name]
    assert any("Background task failed" in m for m in messages), messages


# ── 5. _coerce_response must use isinstance, not duck-typing ────────


class FakeDumper:
    """Looks like a Pydantic model but isn't — `_coerce_response` should
    not route it through `JSONResponse(result.model_dump())`."""

    def model_dump(self):
        return {"oops": 1}


def test_coerce_response_does_not_duck_type_model_dump():
    app = Veloce(openapi_url=None)

    @app.get("/fake")
    async def view(request):
        return FakeDumper()

    client = app.test_client()
    resp = client.get("/fake")
    # FakeDumper is not JSON-serializable and not a Pydantic model. The
    # framework must NOT silently invoke `.model_dump()` and produce
    # `{"oops": 1}`. The only acceptable outcomes are a non-200 (orjson
    # TypeError surfacing as a 500) or a fallback body that does not
    # contain the duck-typed dump.
    assert b'"oops"' not in resp.body, (
        f"_coerce_response duck-typed .model_dump(); body={resp.body!r}"
    )


# ── 6. Protocol: oversize URL → 414 ─────────────────────────────────


def test_protocol_oversize_url_emits_414():
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        long_path = b"/" + (b"a" * (16 * 1024))  # 16 KiB
        assert len(long_path) > MAX_URL_SIZE
        proto.data_received(b"GET " + long_path + b" HTTP/1.1\r\nHost: x\r\n\r\n")

        emitted = b"".join(transport.writes)
        assert emitted.startswith(b"HTTP/1.1 414 "), emitted[:64]
        assert transport.closed is True
    finally:
        loop.close()


# ── 7. Protocol: oversize single header → 431 ───────────────────────


def test_protocol_oversize_single_header_emits_431():
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        big_value = b"v" * (MAX_HEADER_SIZE + 256)  # > 8 KB
        proto.data_received(b"GET / HTTP/1.1\r\nHost: x\r\nX-Huge: " + big_value + b"\r\n\r\n")

        emitted = b"".join(transport.writes)
        assert emitted.startswith(b"HTTP/1.1 431 "), emitted[:64]
        assert transport.closed is True
    finally:
        loop.close()


# ── 8. Protocol: cumulative headers → 431 ───────────────────────────


def test_protocol_cumulative_headers_emit_431():
    loop = asyncio.new_event_loop()
    try:
        proto = HttpProtocol(Veloce(openapi_url=None), loop)
        transport = _FakeTransport()
        proto.connection_made(transport)

        # Each header ~1 KB; need > MAX_TOTAL_HEADERS_SIZE (64 KB) cumulative.
        per_header = b"v" * 1024
        chunks = [b"GET / HTTP/1.1\r\nHost: x\r\n"]
        n_headers = (MAX_TOTAL_HEADERS_SIZE // 1024) + 8
        for i in range(n_headers):
            chunks.append(b"X-Pad-%d: " % i + per_header + b"\r\n")
        chunks.append(b"\r\n")
        proto.data_received(b"".join(chunks))

        emitted = b"".join(transport.writes)
        assert emitted.startswith(b"HTTP/1.1 431 "), emitted[:64]
        assert transport.closed is True
    finally:
        loop.close()


# ── 9. send_robust_async with mixed sync / async receivers ──────────


async def test_send_robust_async_mixed_receivers():
    sig = Signal("mixed")
    fired: list[str] = []

    def sync_recv(sender, **kw):
        fired.append("sync")
        return "sync-ok"

    async def async_raise(sender, **kw):
        fired.append("async-raise")
        raise RuntimeError("boom-async")

    async def async_ok(sender, **kw):
        fired.append("async-ok")
        return "async-ok"

    sig.connect(sync_recv, weak=False)
    sig.connect(async_raise, weak=False)
    sig.connect(async_ok, weak=False)

    results = await sig.send_robust_async("sender")

    assert fired == ["sync", "async-raise", "async-ok"], fired
    assert len(results) == 3
    receivers = [r for r, _ in results]
    values = [v for _, v in results]
    assert sync_recv in receivers
    assert async_raise in receivers
    assert async_ok in receivers
    # The async-raise receiver's value is the captured exception.
    raised_value = next(v for r, v in results if r is async_raise)
    assert isinstance(raised_value, RuntimeError)
    assert "boom-async" in str(raised_value)
    # async_ok awaited cleanly.
    assert "async-ok" in values


# ── 10. send_robust rejects async receivers (no unawaited warning) ──


def test_send_robust_rejects_async_receiver(recwarn):
    sig = Signal("sync-only")

    async def async_recv(sender, **kw):
        return "should-not-be-awaited"

    sig.connect(async_recv, weak=False)
    results = sig.send_robust("sender")

    assert len(results) == 1
    receiver, value = results[0]
    assert receiver is async_recv
    assert isinstance(value, TypeError), value
    # No unawaited-coroutine RuntimeWarning slipped through.
    unawaited = [
        w
        for w in recwarn.list
        if issubclass(w.category, RuntimeWarning) and "never awaited" in str(w.message)
    ]
    assert not unawaited, [str(w.message) for w in unawaited]
