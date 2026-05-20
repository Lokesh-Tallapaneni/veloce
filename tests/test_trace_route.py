"""app.trace — TRACE method route decorator (RFC 9110 §9.3.8)."""

from __future__ import annotations

from veloce import Veloce
from veloce.testclient import TestClient


def test_trace_decorator_registers_route():
    app = Veloce()

    @app.trace("/diag")
    async def diag():
        return {"method": "TRACE"}

    assert "TRACE" in app.get_allowed_methods("/diag")


def test_trace_route_dispatches():
    app = Veloce()

    @app.trace("/echo")
    async def echo():
        return {"traced": True}

    with TestClient(app) as client:
        resp = client.options("/echo")
        # OPTIONS auto-responds; TRACE is advertised in Allow.
        assert "TRACE" in resp.headers["Allow"]


def test_trace_coexists_with_get_on_same_path():
    app = Veloce()

    @app.get("/r")
    async def r_get():
        return {"verb": "get"}

    @app.trace("/r")
    async def r_trace():
        return {"verb": "trace"}

    allowed = app.get_allowed_methods("/r")
    assert "GET" in allowed
    assert "TRACE" in allowed
