"""Array query parameters in OpenAPI — array schema + style/explode (O11)."""

from __future__ import annotations

from veloce import Query, Veloce
from veloce.testclient import TestClient


def _query_param(schema: dict, path: str, name: str) -> dict:
    params = schema["paths"][path]["get"]["parameters"]
    return [p for p in params if p["name"] == name][0]


def test_list_query_param_is_array_schema():
    app = Veloce()

    @app.get("/items")
    async def items(tags: list[str] = Query(default=[])):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    p = _query_param(schema, "/items", "tags")
    assert p["schema"]["type"] == "array"
    assert p["schema"]["items"] == {"type": "string"}


def test_list_query_param_typed_items():
    app = Veloce()

    @app.get("/items")
    async def items(ids: list[int] = Query(default=[])):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    p = _query_param(schema, "/items", "ids")
    assert p["schema"]["items"] == {"type": "integer"}


def test_array_query_param_style_and_explode():
    app = Veloce()

    @app.get("/items")
    async def items(tags: list[str] = Query(default=[])):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    p = _query_param(schema, "/items", "tags")
    assert p["style"] == "form"
    assert p["explode"] is True


def test_scalar_query_param_has_no_style_explode():
    app = Veloce()

    @app.get("/items")
    async def items(name: str = Query(default="")):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    p = _query_param(schema, "/items", "name")
    assert "style" not in p
    assert "explode" not in p


def test_plain_list_query_param_collects_multiple_values():
    app = Veloce()

    @app.get("/items")
    async def items(tags: list[str]):
        return {"tags": tags}

    with TestClient(app) as client:
        # Plain `list[str]` query param collects every repeated value.
        resp = client.get("/items?tags=a&tags=b")
        schema = client.get("/openapi.json").json()

    assert resp.json() == {"tags": ["a", "b"]}
    p = _query_param(schema, "/items", "tags")
    assert p["schema"]["type"] == "array"
