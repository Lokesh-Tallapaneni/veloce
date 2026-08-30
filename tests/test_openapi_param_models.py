"""OpenAPI parameter / form schema fidelity for non-body params.

Non-body parameters (query / path / header / cookie) and form fields
arrive over the wire as raw strings; the resolver coerces each string
through `_coerce_value`. These tests pin `_python_type_to_schema` to the
shapes that string-origin pipeline can actually deliver, and prove the
documented schema is honoured at request time:

- A bare Pydantic model emits `{"type": "string"}`: the resolver parses
  that string as a JSON document into the model (`?tag={"name":"x"}`), so
  the wire shape is a string and a matching value resolves to 200.
- A model nested inside `list` / `dict` / `set`, or inside a union, is NOT
  JSON-decodable (the resolver only decodes a *bare* model), so the schema
  never advertises the model's fields there — the container collapses to a
  bare object / string item, and a model member is dropped from a union.
- A union that includes `str` (or `bytes`) collapses to `{"type": "string"}`
  because smart-mode coercion keeps the string value on that member.
- A union of non-string, non-model members emits an `anyOf` over them; the
  resolver resolves the string to whichever branch matches, so each branch
  is genuinely reachable.
"""

from __future__ import annotations

import json
import urllib.parse
import uuid
from datetime import date, datetime
from typing import Union  # noqa: UP035 — exercises the typing.Union (not PEP 604) origin

from pydantic import BaseModel

from tests.conftest import make_request
from veloce import Body, Cookie, Form, Header, Query, Request, Veloce
from veloce.contrib.openapi import _python_type_to_schema, get_openapi_schema


class _Tag(BaseModel):
    name: str


class _Other(BaseModel):
    value: int


def _make_request(path: str, query_string: str = "") -> Request:
    """A GET, through the shared factory rather than a second spelling of it."""
    return make_request(path=path, query_string=query_string)


def _make_form_request(path: str, body: bytes) -> Request:
    """A urlencoded POST; the two headers are what this module varies."""
    return make_request(
        method="POST",
        path=path,
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


def test_model_query_param_is_a_string_parameter_not_a_request_body() -> None:
    # A model carried by `Query()` is read from the query string as a JSON
    # document at runtime, so the route documents it as a string `parameter`,
    # NOT as a JSON `requestBody` $ref (the model only becomes a request body
    # when it carries no marker or an explicit `Body()`).
    app = Veloce()

    @app.get("/q")
    async def q(request, tag: _Tag = Query()):
        return {"ok": True}

    op = _operation(app, "/q", "get")
    assert "requestBody" not in op
    params = {p["name"]: p for p in op["parameters"]}
    assert params["tag"]["in"] == "query"
    assert params["tag"]["schema"] == {"type": "string"}


def test_model_form_param_is_a_string_form_field_not_a_request_body() -> None:
    app = Veloce()

    @app.post("/f")
    async def f(request, tag: _Tag = Form()):
        return {"ok": True}

    op = _operation(app, "/f")
    field = op["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"][
        "properties"
    ]["tag"]
    assert field == {"type": "string"}


def test_bare_model_param_is_still_a_json_request_body() -> None:
    # No marker → the model is the JSON request body, resolved to a $ref.
    app = Veloce()

    @app.post("/b")
    async def b(request, tag: _Tag):
        return {"ok": True}

    op = _operation(app, "/b")
    assert "requestBody" in op
    assert "_Tag" in get_openapi_schema(app)["components"]["schemas"]


def test_explicit_body_model_is_a_json_request_body() -> None:
    # An explicit `Body()`-marked model is still a JSON request body $ref.

    app = Veloce()

    @app.post("/b2")
    async def b2(request, tag: _Tag = Body()):
        return {"ok": True}

    op = _operation(app, "/b2")
    assert "requestBody" in op
    assert "_Tag" in get_openapi_schema(app)["components"]["schemas"]


def test_header_and_cookie_model_markers_are_string_parameters() -> None:
    # A model carried by Header()/Cookie() is read from that source as a
    # JSON-document string, so it is a string `parameter`, not a requestBody.

    app = Veloce()

    @app.get("/hc")
    async def hc(request, h: _Tag = Header(), c: _Tag = Cookie()):
        return {"ok": True}

    op = _operation(app, "/hc", "get")
    assert "requestBody" not in op
    params = {p["name"]: p for p in op["parameters"]}
    assert params["h"]["in"] == "header"
    assert params["h"]["schema"] == {"type": "string"}
    assert params["c"]["in"] == "cookie"
    assert params["c"]["schema"] == {"type": "string"}


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


def test_dict_of_model_query_param_emits_bare_object() -> None:
    # A model-valued mapping param is not JSON-decodable by the resolver
    # (see test_dict_of_model_query_param_rejects_json_string below), so the
    # schema is a bare object — it must NOT advertise the model's fields as
    # decodable `additionalProperties`.
    app = Veloce()

    @app.get("/lookup")
    async def lookup(request, table: dict[str, _Tag] = Query()):
        return {"ok": True}

    schema = get_openapi_schema(app)
    param = _operation(app, "/lookup", "get")["parameters"][0]
    assert param["schema"] == {"type": "object"}
    assert "_Tag" not in schema["components"]["schemas"]


async def test_dict_of_model_query_param_rejects_json_string() -> None:
    # Ground truth for the schema above: the resolver does not JSON-decode a
    # mapping param's model values, so a JSON-object string is a 422.
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/lookup")
    async def lookup(table: dict[str, _Tag] = Query()):
        return {"ok": True}

    qs = "table=" + urllib.parse.quote('{"a":{"name":"x"}}')
    resp = await app.handle_request(_make_request("/lookup", qs))
    assert resp.status_code == 422


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


def test_model_in_union_is_dropped_leaving_reachable_branch() -> None:
    # A model member is not reachable from a string in a union (the resolver
    # only JSON-decodes a bare model), so it is dropped. `_Tag | int` leaves a
    # single reachable branch — the integer schema — not an anyOf advertising
    # the unreachable model.
    assert _python_type_to_schema(_Tag | int) == {"type": "integer"}


def test_model_in_multi_branch_union_drops_only_the_model() -> None:
    assert _python_type_to_schema(_Tag | int | float) == {
        "anyOf": [{"type": "integer"}, {"type": "number"}]
    }


async def test_model_union_resolves_reachable_branch_and_rejects_model_string() -> None:
    # Ground truth for the schema above: `_Tag | int` resolves an integer
    # string to the int branch (200) but rejects a JSON-object string (422) —
    # so the schema must advertise integer only, never the model branch.
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/u")
    async def u(v: _Tag | int = Query()):
        return {"v": str(v)}

    ok = await app.handle_request(_make_request("/u", "v=123"))
    assert ok.status_code == 200
    bad = await app.handle_request(_make_request("/u", "v=" + urllib.parse.quote('{"name":"x"}')))
    assert bad.status_code == 422


def test_int_bytes_union_collapses_to_string() -> None:
    # `bytes` accepts a string directly (the resolver resolves `?v=abc` to the
    # bytes branch), so a union containing bytes collapses to a plain string.
    assert _python_type_to_schema(int | bytes) == {"type": "string"}


def test_all_model_union_emits_bare_object() -> None:
    # No member is reachable from a string (`A | B` 422s on any string value),
    # so the union documents a bare object, not a string the resolver rejects.
    assert _python_type_to_schema(_Tag | _Other) == {"type": "object"}


async def test_all_model_union_rejects_string_input() -> None:
    # Ground truth for the schema above.
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/u")
    async def u(v: _Tag | _Other = Query()):
        return {"ok": True}

    resp = await app.handle_request(_make_request("/u", "v=" + urllib.parse.quote('{"name":"x"}')))
    assert resp.status_code == 422


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
    # Decoded, not matched: `b'"abc"' in body` holds wherever the value lands,
    # including under a different key than the one the model declares.
    assert json.loads(resp.body)["q"] == "abc"
