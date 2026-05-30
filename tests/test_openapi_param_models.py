"""OpenAPI parameter / form schema completeness for nested models and unions.

Covers `_python_type_to_schema` resolving Pydantic models inside
`list`/`dict`/`set` to `$ref` (registering components.schemas) and
emitting `anyOf` for multi-member unions, plus the registry-less
object-shaped fallback.
"""

from __future__ import annotations

from typing import Union  # noqa: UP035 — exercises the typing.Union (not PEP 604) origin

from pydantic import BaseModel

from veloce import Veloce
from veloce.contrib.openapi import _python_type_to_schema, get_openapi_schema
from veloce.routing.params import Form, Query


class _Tag(BaseModel):
    name: str


def _operation(app: Veloce, path: str, method: str = "post") -> dict:
    return get_openapi_schema(app)["paths"][path][method]


def test_registry_less_model_falls_back_to_object() -> None:
    # Regression guard: no registry → object-shaped, not a bare string.
    assert _python_type_to_schema(_Tag) == {"type": "object"}


def test_registry_model_resolves_to_ref() -> None:
    registry: dict[str, dict] = {}
    assert _python_type_to_schema(_Tag, registry) == {"$ref": "#/components/schemas/_Tag"}
    assert "_Tag" in registry


def test_list_of_model_form_field_emits_array_of_ref() -> None:
    app = Veloce()

    @app.post("/tags")
    async def create(request, tags: list[_Tag] = Form()):
        return {"ok": True}

    schema = get_openapi_schema(app)
    field = _operation(app, "/tags")["requestBody"]["content"]["application/x-www-form-urlencoded"][
        "schema"
    ]["properties"]["tags"]
    assert field == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/_Tag"},
    }
    assert "_Tag" in schema["components"]["schemas"]


def test_dict_of_model_query_param_emits_additional_properties_ref() -> None:
    app = Veloce()

    @app.get("/lookup")
    async def lookup(request, table: dict[str, _Tag] = Query()):
        return {"ok": True}

    schema = get_openapi_schema(app)
    param = _operation(app, "/lookup", "get")["parameters"][0]
    assert param["schema"] == {
        "type": "object",
        "additionalProperties": {"$ref": "#/components/schemas/_Tag"},
    }
    assert "_Tag" in schema["components"]["schemas"]


def test_set_of_model_emits_array_of_ref() -> None:
    registry: dict[str, dict] = {}
    assert _python_type_to_schema(set[_Tag], registry) == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/_Tag"},
    }
    assert "_Tag" in registry


def test_typing_union_multi_member_emits_any_of() -> None:
    # Exercises the `origin is Union` branch (typing.Union, not PEP 604).
    assert _python_type_to_schema(Union[int, str]) == {  # noqa: UP007
        "anyOf": [{"type": "integer"}, {"type": "string"}]
    }


def test_pep604_union_emits_any_of() -> None:
    assert _python_type_to_schema(int | str) == {"anyOf": [{"type": "integer"}, {"type": "string"}]}


def test_optional_multi_member_union_appends_null() -> None:
    assert _python_type_to_schema(int | str | None) == {
        "anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}]
    }


def test_optional_single_member_still_collapses() -> None:
    # int | None keeps the historical collapse to the inner schema.
    assert _python_type_to_schema(int | None) == {"type": "integer"}


def test_union_query_param_emits_any_of_schema() -> None:
    app = Veloce()

    @app.get("/search")
    async def search(request, q: int | str = Query()):
        return {"ok": True}

    param = _operation(app, "/search", "get")["parameters"][0]
    assert param["schema"] == {"anyOf": [{"type": "integer"}, {"type": "string"}]}
