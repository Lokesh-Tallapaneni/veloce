"""Signals primitive + framework integration."""

from __future__ import annotations

import asyncio
import contextvars
import time

import pytest

from veloce import Request, Veloce
from veloce.signals import (
    ANY_SENDER,
    Namespace,
    Signal,
    got_request_exception,
    request_finished,
    request_started,
    request_tearing_down,
)


def _req(path: str = "/x") -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


# ── Signal class ─────────────────────────────────────────────────────


def test_connect_and_send():
    sig = Signal("test")
    calls = []

    def handler(sender, **kwargs):
        calls.append((sender, kwargs))

    sig.connect(handler, weak=False)
    sig.send("sender-x", foo=1)
    assert calls == [("sender-x", {"foo": 1})]


def test_disconnect_stops_firing():
    sig = Signal("test")
    calls = []

    def handler(sender, **kwargs):
        calls.append(1)

    sig.connect(handler, weak=False)
    sig.disconnect(handler)
    sig.send("x")
    assert calls == []


def test_has_receivers_for_true_when_connected():
    sig = Signal()
    assert not sig.has_receivers_for(None)
    sig.connect(lambda s, **kw: None, weak=False)
    assert sig.has_receivers_for(None)


def test_weak_ref_dies_when_owner_collected():
    """Weak-ref receivers don't pin the owner."""
    import gc

    sig = Signal()

    class Owner:
        def handle(self, sender, **kw):
            pass

    o = Owner()
    sig.connect(o.handle, weak=True)
    assert sig.has_receivers_for(None)
    del o
    gc.collect()
    # After GC, the weakref's target is gone; send sees zero receivers.
    sig.send("x")
    assert not sig.has_receivers_for(None)


# ── Namespace factory ────────────────────────────────────────────────


def test_namespace_same_name_returns_same_signal():
    ns = Namespace()
    a = ns.signal("user-registered")
    b = ns.signal("user-registered")
    assert a is b
    assert isinstance(a, Signal)
    assert a.name == "user-registered"


def test_namespace_different_names_are_distinct():
    ns = Namespace()
    a = ns.signal("created")
    b = ns.signal("deleted")
    assert a is not b
    assert a.name == "created"
    assert b.name == "deleted"


def test_namespace_signal_send_and_connect():
    ns = Namespace()
    sig = ns.signal("ping")
    received = []

    sig.connect(lambda sender, **kw: received.append((sender, kw)), weak=False)
    sig.send("origin", n=3)
    assert received == [("origin", {"n": 3})]


def test_namespace_doc_arg_is_accepted():
    ns = Namespace()
    sig = ns.signal("with-doc", doc="a documented signal")
    # doc is accepted for API parity; the cached signal is returned as-is.
    assert ns.signal("with-doc") is sig


# ── Framework integration ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_started_fires_on_dispatch():
    app = Veloce(debug=True, openapi_url=None)
    fired = []

    def on_start(sender, **kw):
        fired.append(("started", sender))

    request_started.connect(on_start, weak=False)
    try:

        @app.get("/x")
        async def x():
            return {}

        await app.handle_request(_req())
        assert fired and fired[0] == ("started", app)
    finally:
        request_started.disconnect(on_start)


@pytest.mark.asyncio
async def test_request_finished_fires_with_response():
    app = Veloce(debug=True, openapi_url=None)
    captured = []

    def on_done(sender, **kw):
        captured.append(kw.get("response"))

    request_finished.connect(on_done, weak=False)
    try:

        @app.get("/x")
        async def x():
            return {"ok": True}

        resp = await app.handle_request(_req())
        assert captured == [resp]
    finally:
        request_finished.disconnect(on_done)


@pytest.mark.asyncio
async def test_request_tearing_down_always_fires():
    """Even on a clean request, teardown signal fires."""
    app = Veloce(debug=True, openapi_url=None)
    fired = []

    def on_tear(sender, **kw):
        fired.append(kw.get("exc"))

    request_tearing_down.connect(on_tear, weak=False)
    try:

        @app.get("/x")
        async def x():
            return {}

        await app.handle_request(_req())
        assert fired == [None]
    finally:
        request_tearing_down.disconnect(on_tear)


@pytest.mark.asyncio
async def test_got_request_exception_fires_on_error():
    app = Veloce(debug=True, openapi_url=None)
    seen = []

    def on_exc(sender, **kw):
        seen.append(kw.get("exception"))

    got_request_exception.connect(on_exc, weak=False)
    try:

        @app.get("/boom")
        async def boom():
            raise RuntimeError("kaboom")

        # Veloce converts to 500; the signal still fires.
        await app.handle_request(_req("/boom"))
        assert seen and isinstance(seen[0], RuntimeError)
    finally:
        got_request_exception.disconnect(on_exc)


# ── send vs send_robust: per-receiver failure handling ──────────────


def test_send_aborts_on_failing_receiver():
    sig = Signal("strict")
    fired: list[str] = []

    def good_a(sender, **kwargs):
        fired.append("a")
        return "a"

    def bad(sender, **kwargs):
        fired.append("bad")
        raise RuntimeError("boom")

    def good_b(sender, **kwargs):
        fired.append("b")
        return "b"

    sig.connect(good_a, weak=False)
    sig.connect(bad, weak=False)
    sig.connect(good_b, weak=False)

    with pytest.raises(RuntimeError, match="boom"):
        sig.send("s")

    # Strict semantics: receivers registered after `bad` never ran.
    assert fired == ["a", "bad"]


def test_send_robust_returns_exceptions_and_continues():
    sig = Signal("robust")
    fired: list[str] = []

    def good_a(sender, **kwargs):
        fired.append("a")
        return "a"

    def bad(sender, **kwargs):
        fired.append("bad")
        raise RuntimeError("boom")

    def good_b(sender, **kwargs):
        fired.append("b")
        return "b"

    sig.connect(good_a, weak=False)
    sig.connect(bad, weak=False)
    sig.connect(good_b, weak=False)

    results = sig.send_robust("s")

    # Every receiver ran, in order.
    assert fired == ["a", "bad", "b"]
    assert len(results) == 3
    assert results[0] == (good_a, "a")
    assert results[1][0] is bad
    assert isinstance(results[1][1], RuntimeError)
    assert str(results[1][1]) == "boom"
    assert results[2] == (good_b, "b")


def test_send_robust_rejects_async_receiver_with_typeerror():
    """Sync send_robust + async receiver → TypeError entry, no unawaited coroutine."""
    import warnings

    sig = Signal("sync-only")

    async def async_handler(sender, **kwargs):
        return "async"

    sig.connect(async_handler, weak=False)

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        results = sig.send_robust("s")

    assert len(results) == 1
    receiver, value = results[0]
    assert receiver is async_handler
    assert isinstance(value, TypeError)
    assert "send_robust_async" in str(value)


async def test_send_robust_async_mixed_sync_async_with_failure():
    """Async send_robust runs every receiver; per-receiver errors are captured."""
    sig = Signal("mixed")
    fired: list[str] = []

    def sync_ok(sender, **kwargs):
        fired.append("sync")
        return "sync-value"

    async def async_bad(sender, **kwargs):
        fired.append("async-bad")
        raise RuntimeError("kaboom")

    async def async_ok(sender, **kwargs):
        fired.append("async-ok")
        return "async-value"

    sig.connect(sync_ok, weak=False)
    sig.connect(async_bad, weak=False)
    sig.connect(async_ok, weak=False)

    results = await sig.send_robust_async("s")

    assert fired == ["sync", "async-bad", "async-ok"]
    assert len(results) == 3
    assert results[0] == (sync_ok, "sync-value")
    assert results[1][0] is async_bad
    assert isinstance(results[1][1], RuntimeError)
    assert str(results[1][1]) == "kaboom"
    assert results[2] == (async_ok, "async-value")


#: How long each probe receiver sleeps. Concurrent dispatch starts them all at
#: once; sequential dispatch spaces the starts a whole delay apart.
_RECEIVER_DELAY = 0.05


# ── asend: concurrent async dispatch ────────────────────────────────


async def test_asend_runs_async_receivers_concurrently():
    sig = Signal("concurrent")
    starts: list[float] = []

    async def make(marker: str):
        async def receiver(sender, **kwargs):
            starts.append(time.perf_counter())
            await asyncio.sleep(_RECEIVER_DELAY)
            return marker

        return receiver

    for marker in ("a", "b", "c"):
        sig.connect(await make(marker), weak=False)

    results = await sig.asend("s")

    # The robust measure is when each receiver *began*, not how long the whole
    # dispatch took: total elapsed jitters under a loaded scheduler, while the
    # spread between concurrently-scheduled starts stays small. Sequential
    # dispatch would space them a whole delay apart.
    assert len(starts) == 3
    assert max(starts) - min(starts) < _RECEIVER_DELAY / 2, (
        f"receivers did not start concurrently: spread={max(starts) - min(starts):.4f}s"
    )
    assert [value for _, value in results] == ["a", "b", "c"]


async def test_asend_mixes_sync_and_async_inline():
    sig = Signal("mixed-asend")
    fired: list[str] = []

    def sync_ok(sender, **kwargs):
        fired.append("sync")
        return "sync-value"

    async def async_ok(sender, **kwargs):
        fired.append("async")
        return "async-value"

    sig.connect(sync_ok, weak=False)
    sig.connect(async_ok, weak=False)

    results = await sig.asend("s")

    assert results[0] == (sync_ok, "sync-value")
    assert results[1] == (async_ok, "async-value")
    # The sync receiver ran inline before the async ones were awaited.
    assert fired[0] == "sync"


async def test_asend_propagates_first_exception():
    sig = Signal("non-robust")

    async def boom(sender, **kwargs):
        raise RuntimeError("kaboom")

    sig.connect(boom, weak=False)

    with pytest.raises(RuntimeError, match="kaboom"):
        await sig.asend("s")


async def test_asend_runs_all_async_receivers_before_raising():
    """asend must not leave async receivers running after it raises.

    The FIRST async receiver raises, but the SECOND must still run to
    completion before asend re-raises. A `return_exceptions=False` gather
    would re-raise immediately and let the second receiver finish in the
    background after teardown.
    """
    sig = Signal("asend-wait-all")
    ran: list[str] = []

    async def first(sender, **kwargs):
        # Yield once so both receivers are concurrently scheduled, then fail.
        await asyncio.sleep(0)
        raise RuntimeError("first-failed")

    async def second(sender, **kwargs):
        # Outlives `first`; must complete before asend returns/raises.
        await asyncio.sleep(0.05)
        ran.append("second")

    sig.connect(first, weak=False)
    sig.connect(second, weak=False)

    with pytest.raises(RuntimeError, match="first-failed"):
        await sig.asend("s")

    # The second receiver finished before asend raised - nothing is left
    # running in the background.
    assert ran == ["second"]


async def test_send_robust_async_concurrent_and_robust():
    sig = Signal("robust-concurrent")
    starts: list[float] = []

    async def ok_a(sender, **kwargs):
        starts.append(time.perf_counter())
        await asyncio.sleep(_RECEIVER_DELAY)
        return "a"

    async def raiser(sender, **kwargs):
        starts.append(time.perf_counter())
        await asyncio.sleep(_RECEIVER_DELAY)
        raise RuntimeError("boom")

    async def ok_b(sender, **kwargs):
        starts.append(time.perf_counter())
        await asyncio.sleep(_RECEIVER_DELAY)
        return "b"

    sig.connect(ok_a, weak=False)
    sig.connect(raiser, weak=False)
    sig.connect(ok_b, weak=False)

    results = await sig.send_robust_async("s")

    # All fired (no cancellation), order preserved.
    assert len(results) == 3
    assert results[0] == (ok_a, "a")
    assert results[1][0] is raiser
    assert isinstance(results[1][1], RuntimeError)
    assert str(results[1][1]) == "boom"
    assert results[2] == (ok_b, "b")
    # Concurrency measured by start spread rather than total elapsed - see the
    # note on `test_asend_runs_async_receivers_concurrently`.
    assert len(starts) == 3
    assert max(starts) - min(starts) < _RECEIVER_DELAY / 2, (
        f"receivers did not start concurrently: spread={max(starts) - min(starts):.4f}s"
    )


async def test_asend_propagates_contextvars_snapshot():
    var: contextvars.ContextVar[str] = contextvars.ContextVar("cv", default="unset")
    sig = Signal("ctx")
    seen: list[str] = []

    async def receiver(sender, **kwargs):
        await asyncio.sleep(0.01)
        seen.append(var.get())
        return var.get()

    sig.connect(receiver, weak=False)
    sig.connect(receiver, weak=False)

    var.set("snapshot")
    results = await sig.asend("s")

    assert seen == ["snapshot", "snapshot"]
    assert [value for _, value in results] == ["snapshot", "snapshot"]


async def test_asend_async_receiver_sees_pre_sync_mutation_context():
    # The dispatch-time snapshot is taken BEFORE sync receivers run, so a sync
    # receiver mutating a ContextVar must not be observed by async receivers.
    var: contextvars.ContextVar[str] = contextvars.ContextVar("cv-mut", default="unset")
    sig = Signal("ctx-mut")
    seen: list[str] = []

    def sync_mutator(sender, **kwargs):
        var.set("mutated")
        return "sync"

    async def async_reader(sender, **kwargs):
        await asyncio.sleep(0.01)
        seen.append(var.get())
        return var.get()

    sig.connect(sync_mutator, weak=False)
    sig.connect(async_reader, weak=False)

    var.set("original")
    results = await sig.asend("s")

    # Async receiver observes the caller's original value, not "mutated".
    assert seen == ["original"]
    assert dict(results)[async_reader] == "original"


async def test_send_robust_async_async_receiver_sees_pre_sync_mutation_context():
    var: contextvars.ContextVar[str] = contextvars.ContextVar("cv-mut-r", default="unset")
    sig = Signal("ctx-mut-robust")
    seen: list[str] = []

    def sync_mutator(sender, **kwargs):
        var.set("mutated")
        return "sync"

    async def async_reader(sender, **kwargs):
        await asyncio.sleep(0.01)
        seen.append(var.get())
        return var.get()

    sig.connect(sync_mutator, weak=False)
    sig.connect(async_reader, weak=False)

    var.set("original")
    results = await sig.send_robust_async("s")

    assert seen == ["original"]
    assert dict(results)[async_reader] == "original"


def test_connect_is_async_classification_does_not_break_sync_paths():
    """The 4-tuple `_subs` change leaves sync send/send_robust unchanged."""
    import warnings

    sig = Signal("classify")

    def sync_ok(sender, **kwargs):
        return "sync-value"

    async def async_handler(sender, **kwargs):
        return "async"

    sig.connect(sync_ok, weak=False)
    sig.connect(async_handler, weak=False)

    # send_robust still closes the async receiver's coroutine and records a
    # TypeError - no RuntimeWarning, identical to the pre-4-tuple behavior.
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        robust = sig.send_robust("s")
    assert robust[0] == (sync_ok, "sync-value")
    assert robust[1][0] is async_handler
    assert isinstance(robust[1][1], TypeError)
    assert "send_robust_async" in str(robust[1][1])


def test_iter_live_targets_prunes_dead_weakref_after_single_send():
    """Both send and send_robust prune dead weakrefs via _iter_live_targets."""
    import gc

    class Owner:
        def handle(self, sender, **kw):
            return "ok"

    # send() path
    sig_a = Signal("prune-send")
    keep = Owner()
    drop = Owner()
    sig_a.connect(keep.handle, weak=True)
    sig_a.connect(drop.handle, weak=True)
    assert len(sig_a._subs) == 2
    del drop
    gc.collect()
    sig_a.send("x")
    assert len(sig_a._subs) == 1

    # send_robust() path
    sig_b = Signal("prune-robust")
    keep2 = Owner()
    drop2 = Owner()
    sig_b.connect(keep2.handle, weak=True)
    sig_b.connect(drop2.handle, weak=True)
    assert len(sig_b._subs) == 2
    del drop2
    gc.collect()
    sig_b.send_robust("x")
    assert len(sig_b._subs) == 1


def test_send_prunes_when_only_receiver_is_dead():
    """A signal whose sole receiver is a dead weakref is pruned by send().

    The lifecycle dispatch calls send() unconditionally rather than guarding
    on has_receivers_for(): the guard never pruned, so a signal left with only
    dead weakrefs would strand them forever. send() now resolves to the empty
    fast-path only when _subs is truly empty, so the dead entry is dropped.
    """
    import gc

    class Owner:
        def handle(self, sender, **kw):
            return "ok"

    sig = Signal("prune-only-dead")
    drop = Owner()
    sig.connect(drop.handle, weak=True)
    assert len(sig._subs) == 1
    assert sig.has_receivers_for("x") is True
    del drop
    gc.collect()
    # The only receiver is now dead; has_receivers_for sees no live target but
    # does not prune. send() fires nothing yet prunes the stranded entry.
    assert sig.has_receivers_for("x") is False
    assert sig.send("x") == []
    assert sig._subs == []


def test_send_robust_logs_failures(caplog):
    sig = Signal("robust-log")

    def bad(sender, **kwargs):
        raise ValueError("nope")

    sig.connect(bad, weak=False)

    with caplog.at_level("WARNING", logger="veloce.signals"):
        results = sig.send_robust("s")

    assert len(results) == 1
    assert isinstance(results[0][1], ValueError)
    assert any(
        rec.name == "veloce.signals" and rec.levelname == "WARNING" for rec in caplog.records
    )
    # The traceback (exc_info) must be attached so operators can debug.
    assert any(rec.exc_info for rec in caplog.records)


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
