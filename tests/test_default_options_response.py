"""app.make_default_options_response."""

from __future__ import annotations

from veloce import Response, Veloce
from veloce.testclient import TestClient


def _allow_set(resp) -> set[str]:
    return {m.strip() for m in resp.headers["Allow"].split(",")}


def test_returns_response_with_200_empty_body():
    app = Veloce()

    @app.get("/x")
    async def x():
        return {}

    resp = app.make_default_options_response("/x")
    assert isinstance(resp, Response)
    assert resp.status_code == 200
    assert resp.body == b""


def test_allow_header_includes_registered_methods():
    app = Veloce()

    @app.get("/items")
    async def get_items():
        return {}

    @app.post("/items")
    async def post_items():
        return {}

    resp = app.make_default_options_response("/items")
    allow = _allow_set(resp)
    assert "GET" in allow
    assert "POST" in allow


def test_allow_header_adds_head_when_get_present():
    app = Veloce()

    @app.get("/x")
    async def x():
        return {}

    assert "HEAD" in _allow_set(app.make_default_options_response("/x"))


def test_allow_header_always_includes_options():
    app = Veloce()

    @app.post("/x")
    async def x():
        return {}

    assert "OPTIONS" in _allow_set(app.make_default_options_response("/x"))


def test_no_head_when_get_absent():
    app = Veloce()

    @app.post("/x")
    async def x():
        return {}

    assert "HEAD" not in _allow_set(app.make_default_options_response("/x"))


def test_dispatcher_uses_default_options_response():
    """Integration: an unhandled OPTIONS gets the auto Allow response."""
    app = Veloce()

    @app.get("/page")
    async def page():
        return {}

    with TestClient(app) as client:
        resp = client.options("/page")
        assert resp.status_code == 200
        allow = {m.strip() for m in resp.headers["Allow"].split(",")}
        assert "GET" in allow
        assert "OPTIONS" in allow
        assert "HEAD" in allow
