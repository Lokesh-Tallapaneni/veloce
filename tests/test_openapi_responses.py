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


# ── O9: container response-model shapes ─────────────────────────────────


def _success_schema(app: Veloce, path: str, method: str = "get") -> dict:
    op = _spec(app)["paths"][path][method]
    return op["responses"]["200"]["content"]["application/json"]["schema"]


def test_response_model_dict_emits_additional_properties_ref():
    app = Veloce(debug=True)

    @app.get("/m", response_model=dict[str, _Item])
    async def m():
        return {}

    assert _success_schema(app, "/m") == {
        "type": "object",
        "additionalProperties": {"$ref": "#/components/schemas/_Item"},
    }


def test_response_model_optional_emits_anyof_with_null():
    app = Veloce(debug=True)

    @app.get("/o", response_model=_Item | None)
    async def o():
        return None

    assert _success_schema(app, "/o") == {
        "anyOf": [{"$ref": "#/components/schemas/_Item"}, {"type": "null"}]
    }


def test_response_model_set_emits_array_of_refs():
    app = Veloce(debug=True)

    @app.get("/s", response_model=set[_Item])
    async def s():
        return set()

    assert _success_schema(app, "/s") == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/_Item"},
    }


def test_response_model_homogeneous_tuple_emits_array():
    app = Veloce(debug=True)

    @app.get("/t", response_model=tuple[_Item, ...])
    async def t():
        return ()

    assert _success_schema(app, "/t") == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/_Item"},
    }


def test_response_model_fixed_tuple_emits_prefix_items():
    app = Veloce(debug=True)

    @app.get("/p", response_model=tuple[_Item, int])
    async def p():
        return ()

    assert _success_schema(app, "/p") == {
        "type": "array",
        "prefixItems": [{"$ref": "#/components/schemas/_Item"}, {"type": "integer"}],
        "minItems": 2,
    }


def test_response_model_union_of_models_emits_anyof():
    app = Veloce(debug=True)

    @app.get("/u", response_model=_Item | _Error)
    async def u():
        return {}

    schema = _success_schema(app, "/u")
    assert schema == {
        "anyOf": [
            {"$ref": "#/components/schemas/_Item"},
            {"$ref": "#/components/schemas/_Error"},
        ]
    }
    assert "_Item" in _spec(app)["components"]["schemas"]
    assert "_Error" in _spec(app)["components"]["schemas"]


def test_response_model_scalar_dict_value():
    app = Veloce(debug=True)

    @app.get("/c", response_model=dict[str, int])
    async def c():
        return {}

    assert _success_schema(app, "/c") == {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    }


def test_response_model_nested_list_of_dict():
    app = Veloce(debug=True)

    @app.get("/n", response_model=list[dict[str, _Item]])
    async def n():
        return []

    assert _success_schema(app, "/n") == {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": {"$ref": "#/components/schemas/_Item"},
        },
    }


def test_response_model_serialization_mode_registers_output_suffix():
    """A model whose serialization shape differs from its validation shape
    (here, via a computed field) is registered under a `<Name>Output` entry
    so the documented response matches what the framework emits."""
    from functools import cached_property

    from pydantic import computed_field

    class _Priced(BaseModel):
        net: float

        @computed_field  # type: ignore[prop-decorator]
        @cached_property
        def gross(self) -> float:
            return self.net * 1.1

    app = Veloce(debug=True)

    @app.get("/priced", response_model=_Priced)
    async def priced():
        return {"net": 1.0}

    schema = _success_schema(app, "/priced")
    assert schema == {"$ref": "#/components/schemas/_PricedOutput"}
    out = _spec(app)["components"]["schemas"]["_PricedOutput"]
    # The computed `gross` field only appears in the serialization shape.
    assert "gross" in out["properties"]
