"""F4 — observability instrumentation hooks.

`app.add_instrumentation(hook)` registers a per-request hook that receives
a `RequestMetrics` record; the request-lifecycle signals also carry the
`Request` so a tracing bridge can correlate start with finish.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from veloce import HTTPException, RequestMetrics, Veloce
from veloce.http.response import StreamingResponse
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

    @app.get("/stream")
    async def stream():
        async def gen():
            yield b"a"
            yield b"b"

        return StreamingResponse(gen())

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
    assert app.instrumentation_hooks == ()


def test_no_hook_skips_the_clock_read():
    """The advertised zero-cost guarantee: with no hook registered the
    request path does not even read `perf_counter`."""
    app = _app()
    with patch("veloce.app.dispatch.time.perf_counter") as clock:
        app.test_client().get("/items/4")
    assert not clock.called


# ── error requests are still instrumented ─────────────────────────────


def test_hook_fires_on_propagated_exception():
    """With PROPAGATE_EXCEPTIONS the exception leaves dispatch uncaught —
    the hook must still record a 500 so error metrics are never dropped."""
    app = Veloce(debug=True, openapi_url=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    @app.get("/explode")
    async def explode():
        raise ValueError("kaboom")

    client = app.test_client()
    with pytest.raises(ValueError, match="kaboom"):
        client.get("/explode")

    assert len(seen) == 1
    assert seen[0].status_code == 500
    assert seen[0].route == "/explode"


def test_405_reports_route_none():
    """A 405 (path exists, wrong method) carries no matched route — it is
    reported like a 404; `status_code` keeps the two apart."""
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    app.test_client().post("/items/5")
    assert seen[0].status_code == 405
    assert seen[0].route is None


# ── streamed flag ─────────────────────────────────────────────────────


def test_streamed_flag_false_for_buffered_response():
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    app.test_client().get("/items/7")
    assert seen[0].streamed is False


def test_streamed_flag_true_for_streaming_response():
    """A `StreamingResponse` body is emitted on the ASGI send path after the
    hook fires; the record must flag this so timing-sensitive consumers can
    skip it."""
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    resp = app.test_client().get("/stream")
    assert resp.body == b"ab"
    assert len(seen) == 1
    assert seen[0].streamed is True
    assert seen[0].route == "/stream"


def test_request_metrics_streamed_defaults_false():
    m = RequestMetrics(method="GET", path="/x", route="/x", status_code=200, duration_ms=1.0)
    assert m.streamed is False
    assert "streamed=False" in repr(m)


# ── error_type attribution ────────────────────────────────────────────


def test_error_type_set_for_unhandled_exception():
    """A 5xx produced by an unhandled raised exception carries the exception
    class name (never the message) so a tracing bridge can attribute it."""
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    app.test_client().get("/crash")
    assert seen[0].status_code == 500
    assert seen[0].error_type == "ValueError"


def test_error_type_set_on_propagated_exception():
    """error_type is recorded even when PROPAGATE_EXCEPTIONS lets the
    exception escape dispatch (the state is set before the re-raise)."""
    app = Veloce(debug=True, openapi_url=None)
    app.config["PROPAGATE_EXCEPTIONS"] = True
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    @app.get("/explode")
    async def explode():
        raise KeyError("boom")

    with pytest.raises(KeyError):
        app.test_client().get("/explode")

    assert len(seen) == 1
    assert seen[0].status_code == 500
    assert seen[0].error_type == "KeyError"


def test_error_type_none_for_success():
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    app.test_client().get("/items/7")
    assert seen[0].error_type is None


def test_error_type_none_for_handled_5xx():
    """A 5xx deliberately returned by a handler (raised HTTPException, no
    unhandled error) is not mislabelled with an error_type."""
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    app.test_client().get("/boom")
    assert seen[0].status_code == 503
    assert seen[0].error_type is None


def test_error_type_none_for_4xx():
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append)

    app.test_client().get("/no-such-path")
    assert seen[0].status_code == 404
    assert seen[0].error_type is None


def test_request_metrics_error_type_defaults_none_and_in_repr():
    m = RequestMetrics(method="GET", path="/x", route="/x", status_code=200, duration_ms=1.0)
    assert m.error_type is None
    assert "error_type=None" in repr(m)


# ── per-hook route-template exclusion ──────────────────────────────────


def test_exclude_routes_skips_matching_hook():
    """A hook registered with exclude_routes is not invoked for a request
    whose matched route template is in the excluded set."""
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append, exclude_routes={"/items/{item_id}"})

    app.test_client().get("/items/7")
    app.test_client().get("/boom")

    routes = [m.route for m in seen]
    assert routes == ["/boom"]


def test_exclude_routes_is_per_hook():
    """Exclusion is scoped to the hook that declared it; other hooks still
    see every request."""
    app = _app()
    excluded: list[RequestMetrics] = []
    everything: list[RequestMetrics] = []
    app.add_instrumentation(excluded.append, exclude_routes={"/items/{item_id}"})
    app.add_instrumentation(everything.append)

    app.test_client().get("/items/7")

    assert excluded == []
    assert len(everything) == 1
    assert everything[0].route == "/items/{item_id}"


def test_exclude_routes_does_not_skip_unmatched_request():
    """A 404/405 has route template None; a named-route exclusion set never
    matches None, so unmatched requests are still delivered."""
    app = _app()
    seen: list[RequestMetrics] = []
    app.add_instrumentation(seen.append, exclude_routes={"/items/{item_id}"})

    app.test_client().get("/no-such-path")
    assert len(seen) == 1
    assert seen[0].route is None
    assert seen[0].status_code == 404


def test_exclude_routes_empty_set_is_inert():
    """An empty exclusion set registers no exclusion entry, leaving the hot
    path free of the membership test."""
    app = _app()
    app.add_instrumentation(lambda m: None, exclude_routes=set())
    assert app._instrumentation_excludes == {}


def test_no_exclude_routes_leaves_excludes_empty():
    app = _app()
    app.add_instrumentation(lambda m: None)
    assert app._instrumentation_excludes == {}


# ── decorator form with arguments ─────────────────────────────────────


def test_add_instrumentation_decorator_with_exclude_routes():
    # `@app.add_instrumentation(exclude_routes=...)` registers the wrapped
    # function and applies the exclusion - the hook fires for an unexcluded
    # route but is skipped for the excluded one.
    app = _app()
    seen: list[RequestMetrics] = []

    @app.add_instrumentation(exclude_routes={"/items/{item_id}"})
    def record(metrics):
        seen.append(metrics)

    # The decorator returns the original function unchanged.
    assert callable(record)
    assert app.instrumentation_hooks == (record,)
    assert app._instrumentation_excludes[record] == frozenset({"/items/{item_id}"})

    client = app.test_client()
    client.get("/items/9")
    assert seen == []
    client.get("/stream")
    assert len(seen) == 1


def test_add_instrumentation_imperative_with_exclude_routes_still_works():
    # The plain imperative call form is preserved alongside the decorator form.
    app = _app()
    seen: list[RequestMetrics] = []
    hook = seen.append
    returned = app.add_instrumentation(hook, exclude_routes={"/items/{item_id}"})
    assert returned is hook
    app.test_client().get("/items/4")
    assert seen == []


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
