"""Query/Path `multiple_of` numeric constraint — JSON Schema `multipleOf`."""

from __future__ import annotations

import pytest

from veloce import Query, Veloce
from veloce.routing.params import Path
from veloce.testclient import TestClient


def test_value_that_is_a_multiple_passes():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(n: int = Query(multiple_of=5)):
        return {"n": n}

    with TestClient(app) as client:
        resp = client.get("/x?n=15")

    assert resp.status_code == 200
    assert resp.json() == {"n": 15}


def test_value_that_is_not_a_multiple_is_rejected():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(n: int = Query(multiple_of=5)):
        return {"n": n}

    with TestClient(app) as client:
        resp = client.get("/x?n=13")

    assert resp.status_code == 422


def test_float_multiple_of():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(price: float = Query(multiple_of=0.25)):
        return {"price": price}

    with TestClient(app) as client:
        assert client.get("/x?price=1.75").status_code == 200
        assert client.get("/x?price=1.80").status_code == 422


def test_multiple_of_emitted_to_openapi():
    app = Veloce()

    @app.get("/x")
    async def x(n: int = Query(multiple_of=10)):
        return {}

    with TestClient(app) as client:
        schema = client.get("/openapi.json").json()

    params = schema["paths"]["/x"]["get"]["parameters"]
    n = [p for p in params if p["name"] == "n"][0]
    assert n["schema"]["multipleOf"] == 10


def test_path_multiple_of():
    app = Veloce(openapi_url=None)

    @app.get("/items/{item_id}")
    async def item(item_id: int = Path(multiple_of=2)):
        return {"item_id": item_id}

    with TestClient(app) as client:
        assert client.get("/items/8").status_code == 200
        assert client.get("/items/7").status_code == 422


def test_multiple_of_zero_rejected_at_construction():
    with pytest.raises(ValueError, match="multiple_of must be positive"):
        Query(multiple_of=0)


def test_multiple_of_positive_construction_ok():
    q = Query(multiple_of=2)
    assert q.multiple_of == 2


def test_multiple_of_negative_construction_allowed():
    # JSON Schema draft 2020-12 §6.2.1 / OpenAPI 3.1 require multipleOf > 0,
    # so negatives are now rejected at declaration time.
    with pytest.raises(ValueError, match="multiple_of must be positive"):
        Query(multiple_of=-3)
