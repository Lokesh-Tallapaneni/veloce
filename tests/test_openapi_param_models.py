"""OpenAPI parameter / form schema fidelity for non-body params.

Non-body parameters (query / path / header / cookie) and form fields
arrive over the wire as raw strings; the resolver coerces each string
through `_coerce_value`. These tests pin `_python_type_to_schema` to the
shapes that string-origin pipeline can actually deliver, and prove the
documented schema is honoured at request time:

- Pydantic models — and models nested inside `list` / `dict` / `set` —
  emit `{"type": "string"}`. The resolver parses that string as a JSON
  document into the model (`?tag={"name":"x"}`), so the wire shape is a
  string and a matching value resolves to 200.
- A union that includes `str` collapses to `{"type": "string"}` because
  smart-mode coercion keeps the string value as the `str` member.
- A union with no string-accepting member emits an `anyOf` over its
  members; the resolver resolves the string to whichever branch matches,
  so each branch is genuinely reachable.
"""

from __future__ import annotations

import urllib.parse
import uuid
from datetime import date, datetime
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


def _make_form_request(path: str, body: bytes) -> Request:
    return Request(
        method="POST",
        path=path,
        query_string="",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "content-length": str(len(body)),
        },
        body=body,
    )


def _operation(app: Veloce, path: str, method: str = "post") -> dict:
    return get_openapi_schema(app)["paths"][path][method]


# ── model-valued non-body params document the JSON-string wire shape ───


def test_bare_model_emits_string() -> None:
    # A model value rides over the wire as a JSON-document string, so the
    # honest schema is `string`, not an object/$ref.
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
    # No phantom component schema registered: the value is a string, not a
    # referenced object.
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


# ── end-to-end: a model param resolves from a JSON-document string ─────


async def test_model_query_param_json_string_resolves_200() -> None:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/q")
    async def q(tag: _Tag = Query()):
        return {"name": tag.name}

    qs = "tag=" + urllib.parse.quote('{"name":"abc"}')
    resp = await app.handle_request(_make_request("/q", qs))
    assert resp.status_code == 200
    assert b'"name":"abc"' in resp.body


async def test_list_of_model_form_field_json_strings_resolve_200() -> None:
    app = Veloce(debug=True, openapi_url=None)

    @app.post("/tags")
    async def create(tags: list[_Tag] = Form()):
        return {"names": [t.name for t in tags]}

    body = (
        b"tags="
        + urllib.parse.quote('{"name":"a"}').encode()
        + b"&tags="
        + urllib.parse.quote('{"name":"b"}').encode()
    )
    resp = await app.handle_request(_make_form_request("/tags", body))
    assert resp.status_code == 200
    assert b'"names":["a","b"]' in resp.body


async def test_model_query_param_non_json_string_returns_422() -> None:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/q")
    async def q(tag: _Tag = Query()):
        return {"name": tag.name}

    resp = await app.handle_request(_make_request("/q", "tag=notjson"))
    assert resp.status_code == 422


# ── unions: string-accepting members collapse to string ────────────────


def test_typing_union_with_str_collapses_to_string() -> None:
    # Exercises the `origin is Union` branch (typing.Union, not PEP 604).
    assert _python_type_to_schema(Union[int, str]) == {"type": "string"}  # noqa: UP007


def test_pep604_union_with_str_collapses_to_string() -> None:
    assert _python_type_to_schema(int | str) == {"type": "string"}


def test_optional_union_with_str_collapses_to_string() -> None:
    assert _python_type_to_schema(int | str | None) == {"type": "string"}


def test_optional_single_member_still_collapses_to_inner() -> None:
    # int | None keeps the historical collapse to the inner scalar schema.
    assert _python_type_to_schema(int | None) == {"type": "integer"}


def test_union_with_str_query_param_emits_string_schema() -> None:
    app = Veloce()

    @app.get("/search")
    async def search(request, q: int | str = Query()):
        return {"ok": True}

    param = _operation(app, "/search", "get")["parameters"][0]
    assert param["schema"] == {"type": "string"}


# ── unions: no string-accepting member emit anyOf ──────────────────────


def test_int_float_union_emits_anyof() -> None:
    assert _python_type_to_schema(int | float) == {
        "anyOf": [{"type": "integer"}, {"type": "number"}]
    }


def test_uuid_int_union_emits_anyof() -> None:
    assert _python_type_to_schema(uuid.UUID | int) == {
        "anyOf": [{"type": "string", "format": "uuid"}, {"type": "integer"}]
    }


def test_date_datetime_union_emits_anyof() -> None:
    assert _python_type_to_schema(date | datetime) == {
        "anyOf": [
            {"type": "string", "format": "date"},
            {"type": "string", "format": "date-time"},
        ]
    }


def test_optional_non_str_union_emits_anyof() -> None:
    # The None member is unwrapped; the remaining branches are reachable.
    assert _python_type_to_schema(int | float | None) == {
        "anyOf": [{"type": "integer"}, {"type": "number"}]
    }


def test_non_str_union_query_param_emits_anyof_schema() -> None:
    app = Veloce()

    @app.get("/measure")
    async def measure(request, value: int | float = Query()):
        return {"ok": True}

    param = _operation(app, "/measure", "get")["parameters"][0]
    assert param["schema"] == {"anyOf": [{"type": "integer"}, {"type": "number"}]}


# ── end-to-end: non-str union branches are reachable at request time ───


async def test_non_str_union_resolves_integer_branch() -> None:
    # The schema documents `anyOf[integer, number]`; an integer-looking
    # string resolves to the int branch, matching the contract.
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/measure")
    async def measure(value: int | float = Query()):
        return {"value": value, "type": type(value).__name__}

    resp = await app.handle_request(_make_request("/measure", "value=123"))
    assert resp.status_code == 200
    assert b'"type":"int"' in resp.body


async def test_non_str_union_resolves_float_branch() -> None:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/measure")
    async def measure(value: int | float = Query()):
        return {"value": value, "type": type(value).__name__}

    resp = await app.handle_request(_make_request("/measure", "value=1.5"))
    assert resp.status_code == 200
    assert b'"type":"float"' in resp.body


async def test_uuid_int_union_resolves_uuid_branch() -> None:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/lookup")
    async def lookup(value: uuid.UUID | int = Query()):
        return {"type": type(value).__name__}

    raw = "12345678-1234-5678-1234-567812345678"
    resp = await app.handle_request(_make_request("/lookup", f"value={raw}"))
    assert resp.status_code == 200
    assert b'"type":"UUID"' in resp.body


# ── end-to-end: a str-containing union resolves to the str branch ──────


async def test_union_with_str_query_param_string_value_resolves_200() -> None:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/search")
    async def search(q: int | str = Query()):
        return {"q": q, "type": type(q).__name__}

    resp = await app.handle_request(_make_request("/search", "q=123"))
    assert resp.status_code == 200
    # The union resolves to the str member, exactly what `string` documents.
    assert b'"q":"123"' in resp.body
    assert b'"type":"str"' in resp.body


async def test_union_with_str_query_param_text_value_resolves_200() -> None:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/search")
    async def search(q: int | str = Query()):
        return {"q": q}

    resp = await app.handle_request(_make_request("/search", "q=abc"))
    assert resp.status_code == 200
    assert b'"abc"' in resp.body
