"""Yield-style dependencies with teardown (D4)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator

import pytest

from veloce import Depends, Request, Veloce


def _req(path: str = "/") -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


@pytest.mark.asyncio
async def test_sync_yield_dependency_teardown_runs():
    app = Veloce(debug=True, openapi_url=None)
    events: list[str] = []

    def db() -> Iterator[str]:
        events.append("setup")
        try:
            yield "session"
        finally:
            events.append("teardown")

    @app.get("/x")
    async def handler(s: str = Depends(db)):
        events.append(f"handler:{s}")
        return {"s": s}

    resp = await app.handle_request(_req("/x"))
    assert resp.status_code == 200
    assert events == ["setup", "handler:session", "teardown"]


@pytest.mark.asyncio
async def test_async_yield_dependency_teardown_runs():
    app = Veloce(debug=True, openapi_url=None)
    events: list[str] = []

    async def db() -> AsyncIterator[str]:
        events.append("setup")
        try:
            yield "async-session"
        finally:
            events.append("teardown")

    @app.get("/x")
    async def handler(s: str = Depends(db)):
        events.append(f"handler:{s}")
        return {"s": s}

    resp = await app.handle_request(_req("/x"))
    assert resp.status_code == 200
    assert events == ["setup", "handler:async-session", "teardown"]


@pytest.mark.asyncio
async def test_teardown_runs_even_on_handler_exception():
    app = Veloce(debug=True, openapi_url=None)
    events: list[str] = []

    def db() -> Iterator[str]:
        events.append("setup")
        try:
            yield "session"
        finally:
            events.append("teardown")

    @app.get("/x")
    async def handler(s: str = Depends(db)):
        events.append("handler:about-to-raise")
        raise RuntimeError("boom")

    resp = await app.handle_request(_req("/x"))
    assert resp.status_code == 500
    # Teardown ran even though the handler raised.
    assert "teardown" in events
    assert events.index("teardown") > events.index("handler:about-to-raise")


@pytest.mark.asyncio
async def test_multiple_yield_deps_tear_down_in_reverse():
    """Most recently set-up dependency must tear down first
    (contextlib.ExitStack semantics)."""
    app = Veloce(debug=True, openapi_url=None)
    events: list[str] = []

    def outer() -> Iterator[str]:
        events.append("outer-setup")
        yield "outer"
        events.append("outer-teardown")

    def inner() -> Iterator[str]:
        events.append("inner-setup")
        yield "inner"
        events.append("inner-teardown")

    @app.get("/x")
    async def handler(o: str = Depends(outer), i: str = Depends(inner)):
        events.append("handler")
        return {"o": o, "i": i}

    await app.handle_request(_req("/x"))
    setup_order = [e for e in events if "setup" in e]
    teardown_order = [e for e in events if "teardown" in e]
    # Set-up in registration order.
    assert setup_order == ["outer-setup", "inner-setup"]
    # Tear down in REVERSE registration order.
    assert teardown_order == ["inner-teardown", "outer-teardown"]


@pytest.mark.asyncio
async def test_mixed_sync_and_async_yield_deps():
    app = Veloce(debug=True, openapi_url=None)
    events: list[str] = []

    def sync_dep() -> Iterator[str]:
        events.append("sync-setup")
        yield "sync-val"
        events.append("sync-teardown")

    async def async_dep() -> AsyncIterator[str]:
        events.append("async-setup")
        yield "async-val"
        events.append("async-teardown")

    @app.get("/x")
    async def handler(a: str = Depends(sync_dep), b: str = Depends(async_dep)):
        events.append("handler")
        return {"a": a, "b": b}

    resp = await app.handle_request(_req("/x"))
    assert resp.status_code == 200
    assert "sync-teardown" in events
    assert "async-teardown" in events


@pytest.mark.asyncio
async def test_yield_dep_raises_if_no_value_yielded():
    """A generator dependency that exits without yielding is a programming
    error — the resolver surfaces it instead of swallowing it silently."""
    app = Veloce(debug=True, openapi_url=None)

    def bad() -> Iterator[str]:
        if False:  # never yields
            yield "x"

    @app.get("/x")
    async def handler(s: str = Depends(bad)):
        return {"s": s}

    resp = await app.handle_request(_req("/x"))
    # The RuntimeError surfaces as a 500 through the default error handler.
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_yield_dep_teardown_exception_does_not_break_response():
    """A teardown that raises must not crash the response cycle."""
    app = Veloce(debug=True, openapi_url=None)
    events: list[str] = []

    def db() -> Iterator[str]:
        events.append("setup")
        try:
            yield "session"
        finally:
            events.append("teardown-trying")
            raise RuntimeError("teardown broke")

    @app.get("/x")
    async def handler(s: str = Depends(db)):
        return {"ok": True, "s": s}

    resp = await app.handle_request(_req("/x"))
    # Response succeeds; teardown error was swallowed.
    assert resp.status_code == 200
    assert b'"ok":true' in resp.body
    assert "teardown-trying" in events


@pytest.mark.asyncio
async def test_yield_dep_state_does_not_leak_across_requests():
    """The resolver's teardown stack must be cleared between requests so
    a stale entry from request N doesn't fire on request N+1."""
    app = Veloce(debug=True, openapi_url=None)
    events: list[str] = []

    def db() -> Iterator[str]:
        events.append("setup")
        try:
            yield "session"
        finally:
            events.append("teardown")

    @app.get("/x")
    async def h(s: str = Depends(db)):
        return {"s": s}

    await app.handle_request(_req("/x"))
    await app.handle_request(_req("/x"))
    # Two requests → two setup/teardown pairs.
    assert events.count("setup") == 2
    assert events.count("teardown") == 2


@pytest.mark.asyncio
async def test_concurrent_requests_keep_independent_teardown_stacks():
    """Two requests in flight at once must not share teardown state: a
    request being dispatched must not wipe an already-parked request's
    pending `yield`-dependency teardowns.

    With a single shared resolver, dispatching /b clears /a's teardown
    stack while /a is parked — the orphaned dependency generator is then
    torn down early (during /b's dispatch, before /a's handler even
    finishes). A per-request resolver keeps /a's teardown until /a
    itself completes, so it must run *after* /a's handler."""
    app = Veloce(debug=True, openapi_url=None)
    events: list[str] = []
    gate = asyncio.Event()

    def dep_a() -> Iterator[str]:
        events.append("setup:a")
        try:
            yield "a"
        finally:
            events.append("teardown:a")

    def dep_b() -> Iterator[str]:
        events.append("setup:b")
        try:
            yield "b"
        finally:
            events.append("teardown:b")

    @app.get("/a")
    async def handler_a(s: str = Depends(dep_a)):
        # Park here, mid-request, so /b is dispatched while /a is still
        # in flight with its teardown already on the resolver.
        await gate.wait()
        events.append("handler:a:done")
        return {"s": s}

    @app.get("/b")
    async def handler_b(s: str = Depends(dep_b)):
        # /b is now fully dispatched (its resolver set up). With a single
        # shared resolver this point is exactly where /a's teardown would
        # have been cleared. Release /a and let both unwind.
        gate.set()
        return {"s": s}

    await asyncio.gather(
        app.handle_request(_req("/a")),
        app.handle_request(_req("/b")),
    )
    # Both requests' teardowns ran exactly once — neither clobbered the other.
    assert events.count("teardown:a") == 1
    assert events.count("teardown:b") == 1
    # /a's teardown must run only after /a's handler completes. A shared
    # resolver tears it down early during /b's dispatch — this fails there.
    assert events.index("teardown:a") > events.index("handler:a:done")


@pytest.mark.asyncio
async def test_teardown_exception_during_error_unwind_is_logged_and_chain_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the handler raises and a teardown's own error-cleanup path raises,
    that bug must surface through the `veloce.dependency` logger instead of
    being silently swallowed, and the remaining teardowns must still run."""
    app = Veloce(debug=True, openapi_url=None)
    events: list[str] = []

    def outer() -> Iterator[str]:
        events.append("outer-setup")
        try:
            yield "outer"
        finally:
            events.append("outer-teardown")

    def bad_inner() -> Iterator[str]:
        events.append("bad-setup")
        try:
            yield "bad"
        except Exception:
            events.append("bad-teardown-raises")
            raise RuntimeError("boom") from None

    @app.get("/x")
    async def handler(o: str = Depends(outer), b: str = Depends(bad_inner)):
        raise ValueError("handler failed")

    with caplog.at_level(logging.ERROR, logger="veloce.dependency"):
        resp = await app.handle_request(_req("/x"))

    assert resp.status_code == 500
    # The bad teardown was reached and raised — the outer teardown still ran
    # afterwards, proving the chain wasn't broken by the one bad teardown.
    assert events == [
        "outer-setup",
        "bad-setup",
        "bad-teardown-raises",
        "outer-teardown",
    ]
    # The RuntimeError must have been logged via the dependency module's
    # logger rather than swallowed by an overly broad contextlib.suppress.
    matches = [
        r
        for r in caplog.records
        if r.name == "veloce.dependency" and "teardown raised" in r.getMessage()
    ]
    assert matches, "expected veloce.dependency to log the teardown failure"
    assert any(isinstance(r.exc_info[1], RuntimeError) if r.exc_info else False for r in matches)
