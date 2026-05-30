"""End-to-end routing, ETag, dependency-cycle, and blueprint guardrails."""

from __future__ import annotations

import os

import pytest

from veloce import Blueprint, Depends, Veloce
from veloce.routing.params import Query
from veloce.testclient import TestClient


def test_greedy_converter_with_trailing_segment_uses_regex_fallback():
    """`{p:path}` followed by a suffix routes through the regex fallback."""
    app = Veloce(openapi_url=None)

    @app.get("/files/{p:path}/info")
    async def handler(p):
        return {"p": p}

    match = app.match("GET", "/files/a/b/c/info")
    assert match is not None
    assert match.path_params == {"p": "a/b/c"}


def test_greedy_converter_as_last_segment_matches_remaining_path():
    app = Veloce(openapi_url=None)

    @app.get("/files/{p:path}")
    async def serve(p: str):
        return {"path": p}

    resp = TestClient(app).get("/files/a/b/c")
    assert resp.status_code == 200
    assert resp.json() == {"path": "a/b/c"}


def test_int_converter_rejects_overlong_segment_with_404():
    app = Veloce(openapi_url=None)

    @app.get("/items/{id:int}")
    async def show(id: int):
        return {"id": id}

    long_digits = "9" * 200
    resp = TestClient(app).get(f"/items/{long_digits}")
    assert resp.status_code == 404


def test_float_converter_accepts_finite_rejects_inf():
    app = Veloce(openapi_url=None)

    @app.get("/items/{x:float}")
    async def show(x: float):
        return {"x": x}

    client = TestClient(app)

    ok = client.get("/items/1.5")
    assert ok.status_code == 200
    assert ok.json() == {"x": 1.5}

    not_finite = client.get("/items/inf")
    assert not_finite.status_code == 404


def test_query_multiple_of_zero_raises_at_declaration():
    """`Query(multiple_of=0)` is invalid — `ParamBase.__init__` rejects it."""
    with pytest.raises(ValueError, match="multiple_of"):
        Query(multiple_of=0)


@pytest.fixture
def static_app(tmp_path):
    asset = tmp_path / "hello.txt"
    asset.write_bytes(b"hello etag\n")
    os.utime(str(asset), (1_700_000_000, 1_700_000_000))

    app = Veloce(openapi_url=None)
    app.mount_static(prefix="/static", directory=str(tmp_path))
    return app


def test_weak_etag_round_trip_with_strong_client_token(static_app):
    """Server emits `W/"..."`; client sends just `"..."` → 304."""
    client = TestClient(static_app)
    first = client.get("/static/hello.txt")
    assert first.status_code == 200
    server_etag = first.headers["etag"]
    assert server_etag.startswith('W/"')

    strong_form = server_etag.removeprefix("W/")
    second = client.get("/static/hello.txt", headers={"if-none-match": strong_form})
    assert second.status_code == 304
    assert second.body == b""


def test_weak_etag_round_trip_with_weak_client_token(static_app):
    """Client echoes the server's `W/"..."` verbatim → 304."""
    client = TestClient(static_app)
    first = client.get("/static/hello.txt")
    server_etag = first.headers["etag"]
    assert server_etag.startswith('W/"')

    second = client.get("/static/hello.txt", headers={"if-none-match": server_etag})
    assert second.status_code == 304
    assert second.body == b""


def test_dependency_cycle_detected_at_registration():
    """`a -> b -> a` raises `ValueError` with the chain when the route is added."""
    app = Veloce(openapi_url=None)

    def a(x=Depends(lambda: None)):
        return x

    def b(x=Depends(a)):
        return x

    # Re-bind `a`'s default to point at `b` to close the cycle.
    a.__defaults__ = (Depends(b),)

    with pytest.raises(ValueError, match="[Cc]ircular dependency"):

        @app.get("/x")
        async def handler(x=Depends(a)):
            return {"x": x}


def test_blueprint_self_register_rejected():
    bp = Blueprint("self_referential")
    with pytest.raises(ValueError, match="itself"):
        bp.register_blueprint(bp)
