"""422 validation errors render as a structured list (V7)."""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Query, Request, Veloce
from veloce.testclient import TestClient


class Item(BaseModel):
    name: str
    price: float


def test_missing_query_param_structured_detail():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(request: Request, n: int = Query()):
        return {}

    with TestClient(app) as client:
        body = client.get("/x").json()

    assert body["detail"] == [
        {
            "loc": ["query", "n"],
            "msg": "Missing required parameter: n",
            "type": "value_error.missing",
        }
    ]


def test_bad_query_param_type_structured_detail():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(request: Request, n: int = Query()):
        return {}

    with TestClient(app) as client:
        resp = client.get("/x?n=notanint")

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"] == ["query", "n"]
    assert detail[0]["type"] == "type_error"


def test_body_model_validation_structured_detail():
    app = Veloce(openapi_url=None)

    @app.post("/items")
    async def create(item: Item):
        return {}

    with TestClient(app) as client:
        resp = client.post("/items", json={"name": "widget"})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list)
    assert any("price" in entry.get("loc", []) for entry in detail)


def test_detail_is_not_a_stringified_repr():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(request: Request, n: int = Query()):
        return {}

    with TestClient(app) as client:
        detail = client.get("/x").json()["detail"]

    # Regression: detail must be a real list, never `str(errors)`.
    assert not isinstance(detail, str)
