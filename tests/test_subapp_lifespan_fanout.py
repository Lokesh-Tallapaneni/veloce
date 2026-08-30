"""Mounted Veloce sub-app lifespan fan-out.

A mounted Veloce sub-app is dispatched through the parent pipeline and never
gets its own ASGI lifespan, so the parent must drive its startup and shutdown:
children start after the parent (and tear down before it, newest-first), and a
child failing mid-fan-out unwinds the already-started children in reverse.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from veloce import Veloce


async def test_mounted_subapp_startup_and_shutdown_fired():
    order: list[str] = []

    parent = Veloce()
    child = Veloce()

    @parent.on_startup
    async def _parent_up():
        order.append("parent-up")

    @parent.on_shutdown
    async def _parent_down():
        order.append("parent-down")

    @child.on_startup
    async def _child_up():
        order.append("child-up")

    @child.on_shutdown
    async def _child_down():
        order.append("child-down")

    parent.mount("/sub", child)

    async with parent.lifespan_context():
        pass

    # Child starts after the parent's own startup, and tears down BEFORE the
    # parent's on_shutdown handlers - reverse of parent-then-children startup, so
    # a shared resource a parent shutdown handler closes is still open while the
    # child releases work against it.
    assert order == ["parent-up", "child-up", "child-down", "parent-down"]


async def test_multiple_children_torn_down_newest_first():
    order: list[str] = []

    parent = Veloce()
    a = Veloce()
    b = Veloce()

    for app_obj, label in ((a, "a"), (b, "b")):

        def _make(label_=label):
            async def _up():
                order.append(f"{label_}-up")

            async def _down():
                order.append(f"{label_}-down")

            return _up, _down

        up, down = _make()
        app_obj.on_startup(up)
        app_obj.on_shutdown(down)

    parent.mount("/a", a)
    parent.mount("/b", b)

    async with parent.lifespan_context():
        pass

    # Startup in mount order, shutdown newest-first.
    assert order == ["a-up", "b-up", "b-down", "a-down"]


async def test_child_startup_failure_unwinds_started_children():
    order: list[str] = []

    parent = Veloce()
    good = Veloce()
    bad = Veloce()

    @good.on_startup
    async def _good_up():
        order.append("good-up")

    @good.on_shutdown
    async def _good_down():
        order.append("good-down")

    @bad.on_startup
    async def _bad_up():
        order.append("bad-up")
        raise RuntimeError("child startup failed")

    parent.mount("/good", good)
    parent.mount("/bad", bad)

    with pytest.raises(RuntimeError, match="child startup failed"):
        async with parent.lifespan_context():
            pass

    # The already-started "good" child is torn down during the unwind.
    assert order == ["good-up", "bad-up", "good-down"]


async def test_non_veloce_asgi_mount_not_lifecycled():
    """A mounted plain-ASGI app is not driven through the lifespan cycle."""
    events: list[str] = []

    async def asgi_app(scope, receive, send):  # pragma: no cover - never called
        events.append(scope["type"])

    parent = Veloce()
    parent.mount("/ext", asgi_app)

    async with parent.lifespan_context():
        pass

    # The ASGI mount owns its own lifecycle; the parent never invoked it.
    assert events == []


async def test_nested_subapp_lifespan_cm_paired():
    """A child's lifespan context manager is entered and exited via the parent."""
    order: list[str] = []

    @contextlib.asynccontextmanager
    async def child_lifespan(app):
        order.append("child-cm-enter")
        try:
            yield
        finally:
            order.append("child-cm-exit")

    parent = Veloce()
    child = Veloce(lifespan=child_lifespan)
    parent.mount("/c", child)

    async with parent.lifespan_context():
        assert order == ["child-cm-enter"]
    assert order == ["child-cm-enter", "child-cm-exit"]


async def test_same_child_mounted_twice_runs_lifecycle_once():
    """A single child instance mounted under multiple prefixes is started and
    shut down exactly once (deduped by identity), not per mount entry.
    """
    counts = {"up": 0, "down": 0}

    parent = Veloce()
    child = Veloce()

    @child.on_startup
    async def _up():
        counts["up"] += 1

    @child.on_shutdown
    async def _down():
        counts["down"] += 1

    parent.mount("/a", child)
    parent.mount("/b", child)  # same instance, second prefix (alias mount)

    async with parent.lifespan_context():
        pass

    assert counts == {"up": 1, "down": 1}


async def test_parent_spawned_tasks_drained_before_child_shutdown():
    """Parent-owned background tasks are cancelled/drained before mounted children
    tear down, so a parent loop cannot touch child state after the child closed."""
    order: list[str] = []
    parent = Veloce()
    child = Veloce()

    @child.on_shutdown
    async def _cdown():
        order.append("child-down")

    async def bg():
        try:
            await asyncio.Event().wait()
        finally:
            order.append("parent-task-stopped")

    parent.mount("/c", child)
    async with parent.lifespan_context():
        parent.spawn(bg(), name="bg")
        for _ in range(5):
            await asyncio.sleep(0)
    assert order == ["parent-task-stopped", "child-down"]
