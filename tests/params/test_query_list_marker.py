"""Query()-marked list parameters collect every repeated value."""

from __future__ import annotations

from veloce import Query, Veloce
from veloce.testclient import TestClient


def test_query_list_collects_multiple_values():
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def items(tags: list[str] = Query(default=[])):
        return {"tags": tags}

    with TestClient(app) as client:
        resp = client.get("/items?tags=a&tags=b&tags=c")

    assert resp.json() == {"tags": ["a", "b", "c"]}


def test_query_list_single_value():
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def items(tags: list[str] = Query(default=[])):
        return {"tags": tags}

    with TestClient(app) as client:
        resp = client.get("/items?tags=only")

    assert resp.json() == {"tags": ["only"]}


def test_query_list_default_when_absent():
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def items(tags: list[str] = Query(default=["fallback"])):
        return {"tags": tags}

    with TestClient(app) as client:
        resp = client.get("/items")

    assert resp.json() == {"tags": ["fallback"]}


def test_query_list_int_items_coerced():
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def items(ids: list[int] = Query(default=[])):
        return {"ids": ids, "total": sum(ids)}

    with TestClient(app) as client:
        resp = client.get("/items?ids=1&ids=2&ids=3")

    assert resp.json() == {"ids": [1, 2, 3], "total": 6}


def test_query_list_required_missing_is_422():
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def items(tags: list[str] = Query()):
        return {"tags": tags}

    with TestClient(app) as client:
        resp = client.get("/items")

    assert resp.status_code == 422
