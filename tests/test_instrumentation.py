"""F4 — observability instrumentation hooks.

`app.add_instrumentation(hook)` registers a per-request hook that receives
a `RequestMetrics` record; the request-lifecycle signals also carry the
`Request` so a tracing bridge can correlate start with finish.
"""

from __future__ import annotations

from veloce import HTTPException, RequestMetrics, Veloce
from veloce.signals import request_finished, request_started


def _app() -> Veloce:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/items/{item_id}")
    async def get_item(item_id: int):
        return {"id": item_id}

    @app.get("/boom")
    async def boom():
        raise HTTPException(503, "down")

    @app.get("/crash")
    async def crash():
        raise ValueError("kaboom")

    return app


# ── basic delivery ────────────────────────────────────────────────────


def test_hook_receives_request_metrics():
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    app.test_client().get("/items/7")

    assert len(seen) == 1
    m = seen[0]
    assert isinstance(m, RequestMetrics)
    assert m.method == "GET"
    assert m.path == "/items/7"
    assert m.route == "/items/{item_id}"
    assert m.status_code == 200
    assert isinstance(m.duration_ms, float)
    assert m.duration_ms >= 0.0


def test_async_hook_supported():
    app = _app()
    seen: list[str] = []

    async def hook(metrics):
        seen.append(metrics.route or "")

    app.add_instrumentation(hook)
    app.test_client().get("/items/1")
    assert seen == ["/items/{item_id}"]


def test_add_instrumentation_works_as_decorator():
    app = _app()
    seen: list[RequestMetrics] = []

    @app.add_instrumentation
    def record(metrics):
        seen.append(metrics)

    app.test_client().get("/items/3")
    assert len(seen) == 1


def test_multiple_hooks_all_invoked():
    app = _app()
    a: list[int] = []
    b: list[int] = []
    app.add_instrumentation(lambda m: a.append(m.status_code))
    app.add_instrumentation(lambda m: b.append(m.status_code))

    app.test_client().get("/items/2")
    assert a == [200]
    assert b == [200]


# ── status codes and route templates ──────────────────────────────────


def test_route_is_none_for_unmatched_path():
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    app.test_client().get("/no/such/path")
    assert seen[0].status_code == 404
    assert seen[0].route is None


def test_status_code_reflects_http_exception():
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    app.test_client().get("/boom")
    assert seen[0].status_code == 503
    assert seen[0].route == "/boom"


def test_status_code_reflects_unhandled_exception():
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    app.test_client().get("/crash")
    assert seen[0].status_code == 500
    assert seen[0].route == "/crash"


# ── robustness ────────────────────────────────────────────────────────


def test_hook_exception_does_not_break_the_response():
    app = _app()

    def bad_hook(metrics):
        raise RuntimeError("instrumentation is broken")

    app.add_instrumentation(bad_hook)
    resp = app.test_client().get("/items/9")
    # The hook blew up, but the response is still delivered intact.
    assert resp.status_code == 200
    assert resp.json() == {"id": 9}


def test_no_hook_registered_is_inert():
    app = _app()
    resp = app.test_client().get("/items/4")
    assert resp.status_code == 200
    assert app._instrumentation == []


# ── lifecycle signals carry the request ───────────────────────────────


def test_request_signals_carry_the_request():
    app = _app()
    started: list[object] = []
    finished: list[object] = []

    def on_start(sender, request=None, **extra):
        started.append(request)

    def on_finish(sender, request=None, response=None, **extra):
        finished.append((request, response))

    request_started.connect(on_start, app)
    request_finished.connect(on_finish, app)
    try:
        app.test_client().get("/items/5")
    finally:
        request_started.disconnect(on_start)
        request_finished.disconnect(on_finish)

    assert len(started) == 1
    assert started[0] is not None
    assert started[0].path == "/items/5"
    # Same Request object flows to request_finished — start/finish correlate.
    assert finished[0][0] is started[0]
    assert finished[0][1] is not None
