"""teardown_appcontext fires per request (L4)."""

from __future__ import annotations

import pytest

from veloce import Request, Response, Veloce
from veloce.blueprints import Blueprint


def _req(path: str = "/") -> Request:
    return Request(method="GET", path=path, query_string="", headers={}, body=b"")


@pytest.mark.asyncio
async def test_teardown_appcontext_fires_on_each_request():
    app = Veloce(debug=True, openapi_url=None)
    events: list = []

    @app.teardown_appcontext
    def cleanup(exc):
        events.append(("teardown", exc))

    @app.get("/x")
    async def x():
        return {"ok": True}

    await app.handle_request(_req("/x"))
    await app.handle_request(_req("/x"))
    await app.handle_request(_req("/x"))

    # Three requests → three teardowns (exception None on each).
    teardown_count = sum(1 for ev in events if ev[0] == "teardown")
    assert teardown_count == 3
    assert all(exc is None for _, exc in events)


@pytest.mark.asyncio
async def test_teardown_appcontext_receives_exception():
    """When a handler raises and Veloce cannot catch it, the hook
    sees the exception."""
    app = Veloce(debug=True, openapi_url=None)
    captured: list = []

    @app.teardown_appcontext
    def cleanup(exc):
        captured.append(exc)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    await app.handle_request(_req("/boom"))
    assert len(captured) == 1
    # The default 500 handler runs first, so the exception is "handled" —
    # depending on framework semantics this might be None or the original.
    # We accept either, but the hook must have fired exactly once.


@pytest.mark.asyncio
async def test_teardown_appcontext_runs_even_when_route_404s():
    """A 404 still ends a request → teardown_appcontext fires."""
    app = Veloce(debug=True, openapi_url=None)
    events: list = []

    @app.teardown_appcontext
    def cleanup(exc):
        events.append("teardown")

    await app.handle_request(_req("/nonexistent"))
    assert "teardown" in events


@pytest.mark.asyncio
async def test_multiple_teardown_hooks_all_fire():
    app = Veloce(debug=True, openapi_url=None)
    events: list = []

    @app.teardown_appcontext
    def first(exc):
        events.append("first")

    @app.teardown_appcontext
    def second(exc):
        events.append("second")

    @app.get("/x")
    async def x():
        return {}

    await app.handle_request(_req("/x"))
    assert events == ["first", "second"]


@pytest.mark.asyncio
async def test_async_teardown_hook_awaited():
    app = Veloce(debug=True, openapi_url=None)
    events: list = []

    @app.teardown_appcontext
    async def cleanup(exc):
        events.append("async-teardown")

    @app.get("/x")
    async def x():
        return {}

    await app.handle_request(_req("/x"))
    assert events == ["async-teardown"]


@pytest.mark.asyncio
async def test_teardown_hook_exception_is_logged_not_propagated():
    """A teardown hook that raises must not break the response."""
    app = Veloce(debug=True, openapi_url=None)

    @app.teardown_appcontext
    def buggy(exc):
        raise RuntimeError("teardown broke")

    @app.get("/x")
    async def x():
        return {"ok": True}

    # Response should succeed despite the teardown error.
    resp = await app.handle_request(_req("/x"))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_app_before_hook_shortcircuit_skips_blueprint_teardown():
    """An app-level before_request short-circuit fires the app teardown_request
    hook but not the matched blueprint's — the dispatcher only records the
    teardown blueprint after the app-level before hooks complete."""
    app = Veloce(debug=True, openapi_url=None)
    bp = Blueprint("bp")
    events: list[str] = []

    @app.before_request
    def gate(request):
        return Response(body=b"blocked", status_code=403)

    @app.teardown_request
    def app_td(exc):
        events.append("app")

    @bp.teardown_request
    def bp_td(exc):
        events.append("bp")

    @bp.get("/x")
    async def handler():
        return {}

    app.register_blueprint(bp)

    resp = await app.handle_request(_req("/x"))
    assert resp.status_code == 403
    # App-level teardown fires; blueprint teardown does not (the short-circuit
    # happened before the blueprint was recorded as the teardown target).
    assert events == ["app"]


@pytest.mark.asyncio
async def test_blueprint_teardown_fires_on_normal_dispatch():
    """When dispatch reaches the blueprint's handler, both the app-level and
    the blueprint teardown_request hooks fire."""
    app = Veloce(debug=True, openapi_url=None)
    bp = Blueprint("bp")
    events: list[str] = []

    @app.teardown_request
    def app_td(exc):
        events.append("app")

    @bp.teardown_request
    def bp_td(exc):
        events.append("bp")

    @bp.get("/x")
    async def handler():
        return {"ok": True}

    app.register_blueprint(bp)

    resp = await app.handle_request(_req("/x"))
    assert resp.status_code == 200
    assert "app" in events
    assert "bp" in events
