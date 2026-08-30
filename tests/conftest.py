"""Shared test fixtures for Veloce test suite."""

import os
import sys

import pytest
from hypothesis import settings

from veloce import Request

# Hypothesis profiles for the parser fuzz suite. The default keeps the
# per-example count modest so the fuzz tests run inside the normal `pytest`
# suite without slowing it down; the `ci` profile (selected by the CI fuzz leg
# via HYPOTHESIS_PROFILE=ci) explores more examples to catch parser
# regressions. A generous deadline avoids flaky timeouts under CPU contention.
settings.register_profile("default", deadline=None)
settings.register_profile("ci", max_examples=400, deadline=None)
settings.load_profile(os.getenv("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture(autouse=True)
def _clear_graceful_drain_latch():
    """Clear the process-wide shutdown latch between tests.

    `HttpProtocol.start_graceful_drain()` sets a module global that makes every
    subsequently-admitted connection quiesce after one request. That is right in
    production, where shutdown is terminal, but a test that drives a server to
    shutdown would otherwise leave every later keep-alive and pipelining test
    serving a single request and failing. Clearing it is one global assignment.
    """
    yield
    from veloce.serving.protocol import HttpProtocol

    HttpProtocol.reset_graceful_drain()


@pytest.fixture(autouse=True)
def _isolate_custom_converters():
    """Restore the process-global converter registry between tests.

    `register_converter` writes a process-global registry, so every test that
    registers one leaks it into every later test. The suite used to compensate
    by hand-numbering names (`slug`, `slug2`, `slug3`) so registrations would
    not collide - a workaround whoever adds the next one has to remember, and
    one that stops working silently when two modules pick the same number.

    Snapshot and restore is one dict copy per test and removes the need for any
    of that. It reads the registry directly (there is no public way to list it)
    but removes through the public `unregister_converter`, so the fixture
    exercises the same inverse a user has.
    """
    from veloce import unregister_converter
    from veloce.routing import converters

    saved = dict(converters._CUSTOM)
    yield
    for name in set(converters._CUSTOM) - set(saved):
        unregister_converter(name)
    converters._CUSTOM.update(saved)


@pytest.fixture(autouse=True)
def _short_close_handshake_timeout(monkeypatch):
    """Shorten the WebSocket close handshake for the suite.

    `close()` on a server-initiated close waits for the peer's reply close frame
    (RFC 6455 Sec. 7.1.1) before dropping the transport, bounded by
    `CLOSE_HANDSHAKE_TIMEOUT = 5.0`. A test driving a raw socket through a fake
    transport has no peer to reply, so every such `close()` blocked the full five
    seconds: **25 tests, 125 seconds** - 93% of the websocket suite's runtime and
    over half the whole suite's, spent waiting for a reply that was never coming.

    No test asserted on the timeout's duration, so nothing is lost by shortening
    it here - and what the timeout actually does is now covered properly, and
    deterministically, in `tests/test_websocket_close_handshake.py`.
    """
    from veloce.websocket import WebSocket

    monkeypatch.setattr(WebSocket, "CLOSE_HANDSHAKE_TIMEOUT", 0.05)


def make_request(
    method: str = "GET",
    path: str = "/",
    headers: dict | None = None,
    body: bytes = b"",
    query_string: str = "",
    **extra,
) -> Request:
    """Build a `Request` for a test.

    The one place the suite constructs a `Request`. Dozens of modules used to
    re-derive this as a private `_req` / `_request` factory - 71 of them
    returning exactly this call with these five arguments, under mutually
    incompatible signatures - so a change to the constructor meant editing all of
    them.

    `**extra` forwards the less common constructor arguments (`app`, `scope`,
    `transport`) that a handful of modules need, so those do not have to fall
    back to building a `Request` by hand.
    """
    return Request(
        method=method,
        path=path,
        query_string=query_string,
        headers=headers or {},
        body=body,
        **extra,
    )


@pytest.fixture(autouse=True)
def _reset_principal():
    """Keep the principal contextvar from leaking between tests.

    This lived in `test_mcp.py` as a module-scoped autouse fixture, so it only
    ever protected that one module - `test_mcp_sse_principal.py` sets a
    principal too and had no such guard. Splitting `test_mcp.py` exposed that:
    with the fixture gone from the module that owned it, tests in another file
    started seeing a principal they never set.

    A contextvar leaking between tests is a suite-wide hazard, so the guard
    belongs here rather than in whichever module happens to remember it.
    """
    from veloce import set_principal

    set_principal(None)
    yield
    set_principal(None)


@pytest.fixture(autouse=True)
def _reset_g():
    """Keep the request-global `g` store from leaking between tests.

    `g` is backed by a contextvar and a sync test runs in the main context, so a
    value one test sets stays bound for the rest of the session. Five tests in
    `test_g_object.py` opened with `g._reset()` for exactly that reason - a
    prologue that defended one module and nothing else, which is the shape
    `_reset_principal` above was moved here to remove.
    """
    from veloce import g

    g._reset()
    yield
    g._reset()


#: Namespace a test may fabricate `sys.modules` entries under, so a dynamically
#: built model has a resolvable `__module__` for `get_type_hints`.
FABRICATED_MODULE_ROOT = "myapp"


@pytest.fixture(autouse=True, scope="session")
def _drop_fabricated_modules():
    """Take the fabricated modules back out of `sys.modules` at session end.

    The write happens at COLLECTION - a model must be a module-level global for
    `get_type_hints` to resolve a handler's string annotations - while the
    module-scoped teardown that used to undo it fired only when a test in that
    module was selected. Under `pytest -k` or `--deselect` the fakes therefore
    stayed in `sys.modules` for the rest of the process. Session scope puts the
    removal on the same lifecycle as the write.
    """
    yield
    prefix = f"{FABRICATED_MODULE_ROOT}."
    for name in [n for n in sys.modules if n == FABRICATED_MODULE_ROOT or n.startswith(prefix)]:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def _no_task_outlives_its_loop():
    """Fail the test that leaves a pending task in `HttpProtocol._active_tasks`.

    That set is process-wide and pruned by each task's done callback, which a
    loop closed with the task still pending never runs. `Veloce._graceful_shutdown`
    then waits on it for `GRACEFUL_DRAIN_TIMEOUT`: one module leaked seventy-one
    of them and the one later test that calls that method spent the full **thirty
    seconds** - seventeen percent of the suite's wall clock - waiting on tasks
    whose loop was gone, while finishing instantly when run on its own.

    Checked here rather than as a source scan because the shape varies: a
    `finally: loop.close()`, a fixture, a helper class owning a loop for its
    lifetime. `tests/_loops.py` is the fix; this is what notices a new one.
    """
    from veloce.serving.protocol import HttpProtocol

    yield
    leaked = [task for task in HttpProtocol._active_tasks if not task.done()]
    HttpProtocol._active_tasks.difference_update(leaked)
    assert not leaked, (
        f"{len(leaked)} task(s) left pending in HttpProtocol._active_tasks. A "
        "loop closed with tasks still on it never runs their done callbacks, so "
        "they stay in that process-wide set for the rest of the session. Use "
        "`tests/_loops.py`'s `protocol_loop()` or `close_drained(loop)`."
    )
