"""OpenAPI: responses= + response_model schema emission (O8, O9)."""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Veloce
from veloce.testclient import TestClient


class _Item(BaseModel):
    id: int
    name: str


class _Error(BaseModel):
    code: int
    message: str


def _spec(app: Veloce) -> dict:
    return TestClient(app).get("/openapi.json").json()


# ── O9: response_model schema in success response ──────────────────────


def test_response_model_emitted_under_success_status():
    app = Veloce(debug=True)

    @app.get("/items/{id}", response_model=_Item)
    async def get_item(id: int):
        return {"id": id, "name": "x"}

    spec = _spec(app)
    op = spec["paths"]["/items/{id}"]["get"]
    # Status defaults to 200.
    assert "200" in op["responses"]
    content = op["responses"]["200"]["content"]["application/json"]
    assert content["schema"] == {"$ref": "#/components/schemas/_Item"}
    # Schema appears under components.
    assert "_Item" in spec["components"]["schemas"]


def test_response_model_list_of_model_emits_array_schema():
    app = Veloce(debug=True)

    @app.get("/items", response_model=list[_Item])
    async def items():
        return []

    spec = _spec(app)
    schema = spec["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert schema == {"type": "array", "items": {"$ref": "#/components/schemas/_Item"}}


def test_route_status_code_changes_default_response_key():
    """`@app.post("/items", status_code=201)` → response listed under "201",
    not "200"."""
    app = Veloce(debug=True)

    @app.post("/items", status_code=201, response_model=_Item)
    async def create(item: _Item):
        return item

    spec = _spec(app)
    op = spec["paths"]["/items"]["post"]
    assert "201" in op["responses"]
    assert "200" not in op["responses"]


# ── O8: extra responses= dict rendered into operation ──────────────────


def test_extra_responses_rendered_as_separate_status_entries():
    app = Veloce(debug=True)

    @app.get(
        "/items/{id}",
        response_model=_Item,
        responses={
            404: {"model": _Error, "description": "Item not found"},
            500: {"description": "Server crashed"},
        },
    )
    async def get_item(id: int):
        return {"id": id, "name": "x"}

    op = _spec(app)["paths"]["/items/{id}"]["get"]
    # Original success entry preserved.
    assert "200" in op["responses"]
    # Extra 404 entry has both description and model schema.
    assert op["responses"]["404"]["description"] == "Item not found"
    assert op["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/_Error"
    }
    # 500 has only description.
    assert op["responses"]["500"]["description"] == "Server crashed"
    assert "content" not in op["responses"]["500"]


def test_responses_passes_through_arbitrary_keys():
    """Keys other than `model`/`description` (e.g. `headers`) are merged
    verbatim into the response object."""
    app = Veloce(debug=True)

    @app.get(
        "/x",
        responses={
            200: {
                "description": "ok",
                "headers": {
                    "X-Rate-Limit": {
                        "description": "Calls per hour remaining",
                        "schema": {"type": "integer"},
                    }
                },
            },
        },
    )
    async def x():
        return {}

    op = _spec(app)["paths"]["/x"]["get"]
    headers = op["responses"]["200"]["headers"]
    assert "X-Rate-Limit" in headers
    assert headers["X-Rate-Limit"]["schema"] == {"type": "integer"}


def test_responses_model_added_to_components():
    """Error models declared in `responses=` end up in components.schemas
    just like the response_model."""
    app = Veloce(debug=True)

    @app.get(
        "/x",
        responses={400: {"model": _Error}, 500: {"model": _Error}},
    )
    async def x():
        return {}

    spec = _spec(app)
    assert "_Error" in spec["components"]["schemas"]


def test_no_response_model_leaves_responses_intact():
    """A route without response_model or responses= still has a default
    200 entry with description but no content."""
    app = Veloce(debug=True)

    @app.get("/x")
    async def x():
        return {}

    op = _spec(app)["paths"]["/x"]["get"]
    assert op["responses"]["200"]
    assert "content" not in op["responses"]["200"]


def test_combination_response_model_plus_extra_error():
    """Common case: success body schema + error model for 422."""
    app = Veloce(debug=True)

    @app.post(
        "/items",
        response_model=_Item,
        status_code=201,
        responses={422: {"model": _Error, "description": "validation failed"}},
    )
    async def create(item: _Item):
        return item

    op = _spec(app)["paths"]["/items"]["post"]
    assert "201" in op["responses"]
    assert "422" in op["responses"]
    success_schema = op["responses"]["201"]["content"]["application/json"]["schema"]
    error_schema = op["responses"]["422"]["content"]["application/json"]["schema"]
    assert success_schema == {"$ref": "#/components/schemas/_Item"}
    assert error_schema == {"$ref": "#/components/schemas/_Error"}
