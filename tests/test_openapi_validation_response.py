"""OpenAPI: automatic 422 (validation) response injection.

The runtime returns a 422 with a `{"detail": [{loc, msg, type}]}` body when
request validation fails. These tests pin that the generated spec advertises
that same response - and only for routes that can actually produce it.
"""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Veloce
from veloce.testclient import TestClient


class _Item(BaseModel):
    id: int
    name: str


class _Error(BaseModel):
    code: int


def _spec(app: Veloce) -> dict:
    return TestClient(app).get("/openapi.json").json()


def test_validatable_path_param_advertises_422():
    app = Veloce(debug=True)

    @app.get("/items/{id}")
    async def get_item(id: int):
        return {"id": id}

    spec = _spec(app)
    op = spec["paths"]["/items/{id}"]["get"]
    assert "422" in op["responses"]
    schema = op["responses"]["422"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/ValidationProblem"}
    assert "ValidationProblem" in spec["components"]["schemas"]


def test_request_body_advertises_422():
    app = Veloce(debug=True)

    @app.post("/items")
    async def create(item: _Item):
        return item

    op = _spec(app)["paths"]["/items"]["post"]
    assert "422" in op["responses"]


def test_problem_schema_matches_runtime_body_shape():
    app = Veloce(debug=True)

    @app.get("/items/{id}")
    async def get_item(id: int):
        return {"id": id}

    spec = _spec(app)
    problem = spec["components"]["schemas"]["ValidationProblem"]
    detail = problem["properties"]["detail"]
    assert detail["type"] == "array"
    item = detail["items"]
    assert set(item["properties"]) == {"loc", "msg", "type"}
    assert item["required"] == ["loc", "msg", "type"]


def test_runtime_422_body_matches_advertised_schema():
    """The handler genuinely returns the advertised `{"detail": [...]}`."""
    app = Veloce(debug=True)

    @app.get("/items/{id}")
    async def get_item(id: int):
        return {"id": id}

    resp = TestClient(app).get("/items/not-an-int")
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body
    assert isinstance(body["detail"], list)


def test_plain_string_path_param_does_not_advertise_422():
    """A bare unconstrained string param never 422s, so no 422 is injected."""
    app = Veloce(debug=True)

    @app.get("/users/{name}")
    async def get_user(name: str):
        return {"name": name}

    op = _spec(app)["paths"]["/users/{name}"]["get"]
    assert "422" not in op["responses"]


def test_no_input_route_does_not_advertise_422():
    app = Veloce(debug=True)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    op = _spec(app)["paths"]["/ping"]["get"]
    assert "422" not in op["responses"]


def test_constrained_string_param_advertises_422():
    from veloce import Query

    app = Veloce(debug=True)

    @app.get("/search")
    async def search(q: str = Query(default="", min_length=3)):
        return {"q": q}

    op = _spec(app)["paths"]["/search"]["get"]
    assert "422" in op["responses"]


def test_user_declared_422_is_not_overwritten():
    app = Veloce(debug=True)

    @app.get(
        "/items/{id}",
        responses={422: {"model": _Error, "description": "custom"}},
    )
    async def get_item(id: int):
        return {"id": id}

    op = _spec(app)["paths"]["/items/{id}"]["get"]
    assert op["responses"]["422"]["description"] == "custom"
    schema = op["responses"]["422"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/_Error"}
    # The canonical schema is not registered when the user supplied their own.
    assert "ValidationProblem" not in _spec(app)["components"]["schemas"]


def test_user_declared_4xx_suppresses_injection():
    app = Veloce(debug=True)

    @app.get("/items/{id}", responses={"4XX": {"description": "any client error"}})
    async def get_item(id: int):
        return {"id": id}

    op = _spec(app)["paths"]["/items/{id}"]["get"]
    assert "422" not in op["responses"]
