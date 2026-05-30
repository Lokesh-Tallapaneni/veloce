"""OpenAPI parameter / form schema fidelity for non-body params.

Non-body parameters (query / path / header / cookie) and form fields
arrive over the wire as raw strings; the resolver coerces each string
through `_coerce_value`. These tests pin `_python_type_to_schema` to the
shapes that string-origin pipeline can actually deliver:

- Pydantic models — and models nested inside `list` / `dict` / `set` —
  collapse to `{"type": "string"}` rather than a `$ref`/object schema,
  because the resolver cannot build a model from a raw string.
- Multi-member unions (`int | str`) collapse to `{"type": "string"}`
  rather than `anyOf`, because every wire value is a string.

The end-to-end tests confirm the documented schema is honoured at
request time — a value matching the emitted schema resolves to 200, it
does not 422.
"""

from __future__ import annotations

from typing import Union  # noqa: UP035 — exercises the typing.Union (not PEP 604) origin

from pydantic import BaseModel

from veloce import Query, Request, Veloce
from veloce.contrib.openapi import _python_type_to_schema, get_openapi_schema
from veloce.routing.params import Form


class _Tag(BaseModel):
    name: str


def _make_request(path: str, query_string: str = "") -> Request:
    return Request(
        method="GET",
        path=path,
        query_string=query_string,
        headers={},
        body=b"",
    )


def _operation(app: Veloce, path: str, method: str = "post") -> dict:
    return get_openapi_schema(app)["paths"][path][method]


# ── model-valued non-body params collapse to string ───────────────────


def test_bare_model_collapses_to_string() -> None:
    # A model can't be reconstructed from a raw query/header/cookie string,
    # so the schema is the honest string shape, not an object/$ref.
    assert _python_type_to_schema(_Tag) == {"type": "string"}


def test_list_of_model_form_field_emits_array_of_string() -> None:
    app = Veloce()

    @app.post("/tags")
    async def create(request, tags: list[_Tag] = Form()):
        return {"ok": True}

    schema = get_openapi_schema(app)
    field = _operation(app, "/tags")["requestBody"]["content"]["application/x-www-form-urlencoded"][
        "schema"
    ]["properties"]["tags"]
    assert field == {"type": "array", "items": {"type": "string"}}
    # No phantom component schema registered for an unreachable model.
    assert "_Tag" not in schema["components"]["schemas"]


def test_dict_of_model_query_param_emits_string_additional_properties() -> None:
    app = Veloce()

    @app.get("/lookup")
    async def lookup(request, table: dict[str, _Tag] = Query()):
        return {"ok": True}

    schema = get_openapi_schema(app)
    param = _operation(app, "/lookup", "get")["parameters"][0]
    assert param["schema"] == {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }
    assert "_Tag" not in schema["components"]["schemas"]


def test_set_of_model_emits_array_of_string() -> None:
    assert _python_type_to_schema(set[_Tag]) == {
        "type": "array",
        "items": {"type": "string"},
    }


# ── multi-member unions collapse to string ─────────────────────────────


def test_typing_union_multi_member_collapses_to_string() -> None:
    # Exercises the `origin is Union` branch (typing.Union, not PEP 604).
    assert _python_type_to_schema(Union[int, str]) == {"type": "string"}  # noqa: UP007


def test_pep604_union_collapses_to_string() -> None:
    assert _python_type_to_schema(int | str) == {"type": "string"}


def test_optional_multi_member_union_collapses_to_string() -> None:
    assert _python_type_to_schema(int | str | None) == {"type": "string"}


def test_optional_single_member_still_collapses_to_inner() -> None:
    # int | None keeps the historical collapse to the inner scalar schema.
    assert _python_type_to_schema(int | None) == {"type": "integer"}


def test_union_query_param_emits_string_schema() -> None:
    app = Veloce()

    @app.get("/search")
    async def search(request, q: int | str = Query()):
        return {"ok": True}

    param = _operation(app, "/search", "get")["parameters"][0]
    assert param["schema"] == {"type": "string"}


# ── end-to-end: the documented schema is reachable at request time ─────


async def test_union_query_param_string_value_resolves_200() -> None:
    # The schema documents `string`; a numeric-looking string value is
    # accepted (resolves to the str member), matching the contract.
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/search")
    async def search(q: int | str = Query()):
        return {"q": q, "type": type(q).__name__}

    resp = await app.handle_request(_make_request("/search", "q=123"))
    assert resp.status_code == 200
    # The union resolves to the str member, exactly what `string` documents.
    assert b'"q":"123"' in resp.body
    assert b'"type":"str"' in resp.body


async def test_union_query_param_text_value_resolves_200() -> None:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/search")
    async def search(q: int | str = Query()):
        return {"q": q}

    resp = await app.handle_request(_make_request("/search", "q=abc"))
    assert resp.status_code == 200
    assert b'"abc"' in resp.body
