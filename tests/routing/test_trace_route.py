"""app.trace — TRACE method route decorator (RFC 9110 §9.3.8).

`test_trace_route_dispatches` used to issue an **OPTIONS** request and assert the
`Allow` header, so despite its name no TRACE request was dispatched by it — or by
anything else in the suite. `@app.trace` could have been broken end to end and
every test here stayed green, because all three only ever asked the router what
it *advertised*.

`TestClient` has no `.trace()` shortcut, which is presumably why the original
reached for OPTIONS; `client.request("TRACE", ...)` is the supported way and is
what these use.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.testclient import TestClient


def _traced_app(path: str = "/echo"):
    app = Veloce(openapi_url=None)
    ran: list[str] = []

    @app.trace(path)
    async def echo():
        ran.append("trace")
        return {"traced": True}

    return app, ran


# ── the route is registered ──────────────────────────────────────────


def test_trace_decorator_registers_route():
    app, _ = _traced_app("/diag")
    assert "TRACE" in app.get_allowed_methods("/diag")


def test_trace_is_advertised_in_allow():
    """What the old dispatch test actually checked; kept, under its real name."""
    app, _ = _traced_app()
    with TestClient(app) as client:
        assert "TRACE" in client.options("/echo").headers["Allow"]


# ── and a TRACE request actually reaches the handler ─────────────────


def test_a_trace_request_reaches_the_handler():
    """The defect: no TRACE request was dispatched anywhere in the suite."""
    app, ran = _traced_app()
    with TestClient(app) as client:
        client.request("TRACE", "/echo")
    assert ran == ["trace"]


def test_a_trace_request_returns_the_handler_response():
    app, _ = _traced_app()
    with TestClient(app) as client:
        resp = client.request("TRACE", "/echo")
    assert resp.status_code == 200
    assert resp.json() == {"traced": True}


def test_a_trace_request_to_an_unregistered_path_is_a_404():
    app, _ = _traced_app()
    with TestClient(app) as client:
        assert client.request("TRACE", "/nowhere").status_code == 404


def test_a_get_to_a_trace_only_route_is_refused():
    """The route answers TRACE and nothing else."""
    app, ran = _traced_app()
    with TestClient(app) as client:
        assert client.get("/echo").status_code == 405
    assert ran == []


# ── coexistence with another method on the same path ─────────────────


def _shared_path_app():
    app = Veloce(openapi_url=None)
    seen: list[str] = []

    @app.get("/r")
    async def r_get():
        seen.append("get")
        return {"verb": "get"}

    @app.trace("/r")
    async def r_trace():
        seen.append("trace")
        return {"verb": "trace"}

    return app, seen


def test_trace_coexists_with_get_on_same_path():
    app, _ = _shared_path_app()
    allowed = app.get_allowed_methods("/r")
    assert "GET" in allowed
    assert "TRACE" in allowed


@pytest.mark.parametrize(("method", "expected"), [("GET", "get"), ("TRACE", "trace")])
def test_each_method_reaches_its_own_handler(method, expected):
    """Advertising both is not the same as routing each to the right one."""
    app, seen = _shared_path_app()
    with TestClient(app) as client:
        resp = client.request(method, "/r")
    assert resp.json() == {"verb": expected}
    assert seen == [expected]
