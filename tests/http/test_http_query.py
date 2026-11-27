"""The HTTP QUERY method routing (RFC 10008) via the `@app.query` decorator.

QUERY is a safe, idempotent request that carries a body — the body of POST with
the read-only guarantees of GET. These tests cover routing, body binding, method
isolation (no HEAD alias), and that a QUERY route does not emit an invalid
`query` operation into the OpenAPI 3.1 document.
"""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Veloce
from veloce.testclient import TestClient


class _Search(BaseModel):
    term: str
    limit: int = 10


def test_query_route_dispatches_with_json_body():
    app = Veloce()

    @app.query("/search")
    async def search(q: _Search) -> dict:
        return {"term": q.term, "limit": q.limit}

    client = TestClient(app)
    resp = client.request("QUERY", "/search", json={"term": "veloce", "limit": 5})
    assert resp.status_code == 200
    assert resp.json() == {"term": "veloce", "limit": 5}


def test_query_validates_body_like_post():
    app = Veloce()

    @app.query("/search")
    async def search(q: _Search) -> dict:
        return {"term": q.term}

    client = TestClient(app)
    # Missing required field -> 422, same validation pipeline as POST.
    resp = client.request("QUERY", "/search", json={"limit": 3})
    assert resp.status_code == 422


def test_query_and_get_coexist_on_same_path():
    app = Veloce()

    @app.get("/things")
    async def list_things() -> dict:
        return {"via": "get"}

    @app.query("/things")
    async def query_things(q: _Search) -> dict:
        return {"via": "query", "term": q.term}

    client = TestClient(app)
    assert client.get("/things").json() == {"via": "get"}
    assert client.request("QUERY", "/things", json={"term": "x"}).json() == {
        "via": "query",
        "term": "x",
    }


def test_query_wrong_method_405_advertises_query():
    app = Veloce()

    @app.query("/only-query")
    async def only_query(q: _Search) -> dict:
        return {"ok": True}

    client = TestClient(app)
    resp = client.post("/only-query", json={"term": "x"})
    assert resp.status_code == 405
    assert "QUERY" in resp.headers.get("allow", "")


def test_query_is_safe_no_head_alias():
    # QUERY must not gain the HEAD alias GET carries (RFC 9110 Sec. 9.3.2 is
    # GET-specific); a QUERY-only path has no HEAD handler.
    app = Veloce()

    @app.query("/q")
    async def q(body: _Search) -> dict:
        return {}

    client = TestClient(app)
    resp = client.request("HEAD", "/q")
    assert resp.status_code == 405


def test_query_route_excluded_from_openapi_31():
    # OpenAPI 3.1 has no `query` Path Item field; the QUERY route must not emit
    # an invalid `query` operation (native 3.2 support is a follow-up).
    app = Veloce()

    @app.get("/items")
    async def items() -> dict:
        return {}

    @app.query("/items")
    async def query_items(q: _Search) -> dict:
        return {}

    schema = app.openapi()
    assert "get" in schema["paths"]["/items"]
    assert "query" not in schema["paths"]["/items"]


def test_query_raw_content_body():
    app = Veloce()

    @app.query("/search")
    async def search(q: _Search) -> dict:
        return {"term": q.term}

    client = TestClient(app)
    resp = client.request(
        "QUERY",
        "/search",
        content=b'{"term":"ok"}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"term": "ok"}
