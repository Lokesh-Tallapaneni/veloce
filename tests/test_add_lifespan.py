"""`app.add_lifespan()` — additional scoped resources beyond `lifespan=`.

`lifespan=` is a single constructor slot owned by the application, so a plugin
had to split setup and teardown across `on_startup`/`on_shutdown` and lose the
paired `try/finally` between them. Registered lifespans share the app's exit
stack, so they inherit its reverse-order teardown and partial-startup unwind.
"""

from __future__ import annotations

import contextlib

import pytest

from veloce import Veloce
from veloce.testclient import TestClient


def test_registered_lifespans_run_and_tear_down_in_reverse():
    order: list[str] = []

    @contextlib.asynccontextmanager
    async def first(app):
        order.append("first-enter")
        yield
        order.append("first-exit")

    @contextlib.asynccontextmanager
    async def second(app):
        order.append("second-enter")
        yield
        order.append("second-exit")

    app = Veloce(openapi_url=None)
    app.add_lifespan(first)
    app.add_lifespan(second)

    @app.get("/")
    async def index():
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/")

    assert order == ["first-enter", "second-enter", "second-exit", "first-exit"]


def test_the_app_lifespan_still_outlives_every_registered_one():
    """A plugin resource may depend on what `lifespan=` provided, so the app's
    own lifespan must be entered first and exited last."""
    order: list[str] = []

    @contextlib.asynccontextmanager
    async def app_lifespan(app):
        order.append("app-enter")
        yield
        order.append("app-exit")

    @contextlib.asynccontextmanager
    async def plugin(app):
        order.append("plugin-enter")
        yield
        order.append("plugin-exit")

    app = Veloce(openapi_url=None, lifespan=app_lifespan)
    app.add_lifespan(plugin)

    @app.get("/")
    async def index():
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/")

    assert order == ["app-enter", "plugin-enter", "plugin-exit", "app-exit"]


def test_a_failure_part_way_through_startup_unwinds_what_was_entered():
    order: list[str] = []

    @contextlib.asynccontextmanager
    async def good(app):
        order.append("good-enter")
        try:
            yield
        finally:
            order.append("good-exit")

    @contextlib.asynccontextmanager
    async def broken(app):
        order.append("broken-enter")
        raise RuntimeError("cannot connect")
        yield  # pragma: no cover

    app = Veloce(openapi_url=None)
    app.add_lifespan(good)
    app.add_lifespan(broken)

    @app.get("/")
    async def index():
        return {"ok": True}

    with pytest.raises(RuntimeError, match="cannot connect"), TestClient(app):
        pass

    # `good` was entered, so it must be torn down even though startup failed.
    assert order == ["good-enter", "broken-enter", "good-exit"]


def test_the_app_is_passed_to_the_factory():
    seen: list[Veloce] = []

    @contextlib.asynccontextmanager
    async def capture(app):
        seen.append(app)
        yield

    app = Veloce(openapi_url=None)
    app.add_lifespan(capture)

    @app.get("/")
    async def index():
        return {"ok": True}

    with TestClient(app):
        pass

    assert seen == [app]


def test_add_lifespan_returns_the_factory_so_it_can_decorate():
    app = Veloce(openapi_url=None)

    @app.add_lifespan
    @contextlib.asynccontextmanager
    async def resource(app):
        yield

    assert callable(resource)


def test_a_plugin_can_own_a_paired_resource():
    """The gap this closes: setup and teardown in one `try/finally`."""
    events: list[str] = []

    class BrokerPlugin:
        name = "broker"

        def install(self, app):
            app.add_lifespan(self.lifespan)

        @contextlib.asynccontextmanager
        async def lifespan(self, app):
            events.append("connect")
            self.conn = object()
            try:
                yield
            finally:
                events.append("close")

    app = Veloce(openapi_url=None)
    plugin = app.install(BrokerPlugin())

    @app.get("/")
    async def index():
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/")
        assert plugin.conn is not None

    assert events == ["connect", "close"]
    assert app.extensions["broker"] is plugin
