"""A grouped model is published by both doors, and both bind it.

`Annotated[Filters, Query(group=True)]` reads each of the model's fields off its
own query key rather than looking for one key named `filters`. The HTTP path did
that correctly. Nothing else knew the kind existed: `describe_slot` returned
`None` for it, and every lowering that asks it what a parameter is therefore saw
nothing.

So one declaration produced three different answers:

    HTTP   GET /items?limit=5&tag=abc   {"limit":5,"tag":"abc"}
    OpenAPI                             "parameters": null
    MCP    inputSchema                  {}
    MCP    call {"limit":5}             {"detail":"Internal Server Error"}

A documented, working parameter form was invisible in the API document, absent
from the tool schema an agent reads, and a 500 when an agent guessed the shape
anyway. `describe_slot` now classifies the kind and reports the field walk, so
both lowerings expand it into the same wire parameters the resolver reads.

The field's declared schema comes from the model, not from the bare annotation,
so `Field(ge=1, le=100)` reaches both published contracts - otherwise the schema
advertises a wider range than the handler accepts, and a well-behaved agent gets
refused for sending a value the tool told it was allowed.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel, Field

import veloce.contrib.openapi as openapi_module
from veloce import Cookie, Header, Query, Veloce
from veloce.testclient import TestClient

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"},
    },
}


class Filters(BaseModel):
    limit: int = Field(10, ge=1, le=100, description="Page size")
    tag: list[str] = Field(default_factory=list)
    sort_by: str = Field("id", alias="sortBy")


class Required(BaseModel):
    q: str


class Inner(BaseModel):
    a: int = 1


class Outer(BaseModel):
    inner: Inner = Field(default_factory=Inner)
    limit: int = Field(10, ge=1)


class Payload(BaseModel):
    a: int


def _app(model=Filters, marker=None):
    app = Veloce(title="Grouped", version="1.0.0")

    async def items(filters=marker or Query(group=True)) -> dict:
        return dict(filters.model_dump(by_alias=True))

    # Bound as objects rather than written in the signature: this module uses
    # PEP 563, so a local name in an annotation would not resolve.
    items.__annotations__ = {"filters": model, "return": dict}
    app.get("/items", expose_as_mcp_tool=True, mcp_description="List items")(items)
    app.mount_mcp(transport="http", path="/mcp")
    return app


def _parameters(app: Veloce) -> dict[str, dict]:
    return {p["name"]: p for p in app.openapi()["paths"]["/items"]["get"]["parameters"]}


def _mcp(app: Veloce) -> TestClient:
    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    return client


def _input_schema(app: Veloce) -> dict:
    response = _mcp(app).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json"},
    )
    return response.json()["result"]["tools"][0]["inputSchema"]


def _call(app: Veloce, arguments: dict) -> dict:
    response = _mcp(app).post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "items", "arguments": arguments},
        },
        headers={"Accept": "application/json"},
    )
    return response.json()["result"]


# ── the group is published at all ────────────────────────────────────


def test_openapi_documents_the_fields():
    """The defect: `parameters` was absent entirely."""
    assert set(_parameters(_app())) == {"limit", "tag", "sortBy"}


def test_the_tool_schema_declares_the_fields():
    """The defect: `inputSchema` had no properties."""
    assert set(_input_schema(_app())["properties"]) == {"limit", "tag", "sortBy"}


def test_neither_door_publishes_the_parameter_name():
    """`filters` is a Python name; no caller ever sends it."""
    assert "filters" not in _parameters(_app())
    assert "filters" not in _input_schema(_app())["properties"]


def test_the_two_doors_publish_the_same_names():
    app = _app()
    assert set(_parameters(app)) == set(_input_schema(app)["properties"])


# ── a call works through both doors ──────────────────────────────────


def test_http_still_binds_the_group():
    """The path that already worked must keep working."""
    body = TestClient(_app()).get("/items?limit=5&tag=a&tag=b&sortBy=name").json()
    assert body == {"limit": 5, "tag": ["a", "b"], "sortBy": "name"}


def test_a_tool_call_binds_the_group():
    """The defect: this raised `AttributeError` and returned a 500."""
    result = _call(_app(), {"limit": 5, "tag": ["a", "b"], "sortBy": "name"})
    assert result["content"][0]["text"] == '{"limit":5,"tag":["a","b"],"sortBy":"name"}'


def test_the_two_doors_return_the_same_body():
    app = _app()
    http = TestClient(app).get("/items?limit=5&tag=a&tag=b&sortBy=name").text
    assert (
        _call(app, {"limit": 5, "tag": ["a", "b"], "sortBy": "name"})["content"][0]["text"] == http
    )


def test_a_tool_call_with_no_arguments_uses_the_defaults():
    result = _call(_app(), {})
    assert result["content"][0]["text"] == '{"limit":10,"tag":[],"sortBy":"id"}'


def test_a_partial_tool_call_fills_the_rest_from_defaults():
    result = _call(_app(), {"limit": 3})
    assert result["content"][0]["text"] == '{"limit":3,"tag":[],"sortBy":"id"}'


# ── the declared constraints reach both contracts ────────────────────


@pytest.mark.parametrize("keyword", ["minimum", "maximum", "default", "description"])
def test_openapi_carries_the_field_constraints(keyword):
    """Rebuilt from the annotation alone, the schema advertised a wider range."""
    assert keyword in _parameters(_app())["limit"]["schema"]


@pytest.mark.parametrize("keyword", ["minimum", "maximum", "default", "description"])
def test_the_tool_schema_carries_the_field_constraints(keyword):
    assert keyword in _input_schema(_app())["properties"]["limit"]


def test_the_two_doors_publish_the_same_schema_for_a_field():
    app = _app()
    assert _parameters(app)["limit"]["schema"] == _input_schema(app)["properties"]["limit"]


def test_a_value_outside_the_published_range_is_refused_on_both_doors():
    """The point of publishing the constraint: neither door accepts 999."""
    app = _app()
    assert TestClient(app).get("/items?limit=999").status_code == 422
    assert _call(app, {"limit": 999})["isError"] is True


def test_a_value_inside_the_range_is_accepted_on_both_doors():
    app = _app()
    assert TestClient(app).get("/items?limit=100").status_code == 200
    assert _call(app, {"limit": 100}).get("isError") is not True


# ── required, default, and alias are reported identically ────────────


def test_a_field_with_a_default_is_optional_on_both_doors():
    app = _app()
    assert _parameters(app)["limit"]["required"] is False
    assert "limit" not in _input_schema(app).get("required", [])


def test_an_aliased_field_with_a_default_is_optional():
    """Keyed by field name, the default was missed and the alias read required."""
    assert _parameters(_app())["sortBy"]["required"] is False


def test_a_field_with_no_default_is_required_on_both_doors():
    app = _app(Required)
    assert _parameters(app)["q"]["required"] is True
    assert "q" in _input_schema(app)["required"]


def test_a_missing_required_field_is_refused_on_both_doors():
    app = _app(Required)
    assert TestClient(app).get("/items").status_code == 422
    assert _call(app, {})["isError"] is True


def test_an_aliased_field_binds_by_its_alias_on_both_doors():
    app = _app()
    assert TestClient(app).get("/items?sortBy=name").json()["sortBy"] == "name"
    assert '"sortBy":"name"' in _call(app, {"sortBy": "name"})["content"][0]["text"]


def test_the_python_field_name_is_not_accepted_by_either_door():
    """`sort_by` is not the wire key; sending it must not silently bind."""
    app = _app()
    assert TestClient(app).get("/items?sort_by=name").json()["sortBy"] == "id"
    assert '"sortBy":"id"' in _call(app, {"sort_by": "name"})["content"][0]["text"]


# ── a list field keeps its array shape ───────────────────────────────


def test_a_list_field_is_an_array_on_both_doors():
    app = _app()
    assert _parameters(app)["tag"]["schema"]["type"] == "array"
    assert _input_schema(app)["properties"]["tag"]["type"] == "array"


def test_a_list_query_parameter_declares_form_explode():
    """OpenAPI 3.1 Sec. 4.8.12.1 - `?tag=a&tag=b` needs the explicit style."""
    tag = _parameters(_app())["tag"]
    assert tag["style"] == "form"
    assert tag["explode"] is True


def test_a_scalar_sent_for_a_list_field_is_refused_by_the_tool():
    assert _call(_app(), {"tag": "a"})["isError"] is True


# ── the other group locations ────────────────────────────────────────


@pytest.mark.parametrize(
    ("marker", "location"),
    [(Header(group=True), "header"), (Cookie(group=True), "cookie")],
)
def test_a_group_in_another_location_is_documented(marker, location):
    app = _app(Filters, marker)
    assert {p["in"] for p in app.openapi()["paths"]["/items"]["get"]["parameters"]} == {location}


def test_a_header_group_still_binds_over_http():
    app = _app(Filters, Header(group=True))
    body = TestClient(app).get("/items", headers={"limit": "7", "sortBy": "name"}).json()
    assert body["limit"] == 7
    assert body["sortBy"] == "name"


# ── a nested model falls back rather than publishing a $ref ──────────


def test_a_nested_field_does_not_publish_a_ref_in_a_parameter():
    """A parameter schema cannot carry a `$ref`; the annotation is used instead."""
    app = _app(Outer)
    assert "$ref" not in str(_parameters(app)["inner"]["schema"])
    # The sibling scalar still gets its declared constraint.
    assert _parameters(app)["limit"]["schema"]["minimum"] == 1


# ── an app that uses no group is untouched ───────────────────────────


def test_an_ordinary_query_parameter_is_unchanged():
    app = Veloce(title="Plain", version="1.0.0")

    @app.get("/x", expose_as_mcp_tool=True, mcp_description="x")
    async def x(limit: int = 10) -> dict:
        return {"limit": limit}

    app.mount_mcp(transport="http", path="/mcp")
    parameters = app.openapi()["paths"]["/x"]["get"]["parameters"]
    assert parameters == [
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "default": 10},
        }
    ]


def test_a_body_model_is_not_treated_as_a_group():
    """`model` on a descriptor still means "body model" everywhere else."""
    app = Veloce(title="Body", version="1.0.0")

    @app.post("/y")
    async def y(payload: Payload) -> dict:
        return {"a": payload.a}

    operation = app.openapi()["paths"]["/y"]["post"]
    assert "requestBody" in operation
    assert not operation.get("parameters")
    assert TestClient(app).post("/y", json={"a": 1}).json() == {"a": 1}


class Inner(BaseModel):
    v: int = 1


class Nested(BaseModel):
    inner: Inner = Field(default_factory=Inner)


# ── a failed field walk is reported, not silently lax ────────────────


def test_a_failed_field_walk_is_reported(monkeypatch, caplog):
    """Falling back is lossy, and it used to look like the `$ref` case.

    A model the property walk cannot introspect drops every constraint above
    while the resolver goes on enforcing them, so the document understates the
    server with nothing said.
    """

    def explode(model):
        raise RuntimeError("introspection moved")

    openapi_module._grouped_model_properties.cache_clear()
    monkeypatch.setattr(openapi_module, "_grouped_model_properties", explode)
    with caplog.at_level(logging.WARNING, logger="veloce.contrib.openapi"):
        schema = _parameters(_app())["limit"]["schema"]

    assert any("grouped field" in record.getMessage() for record in caplog.records)
    # Still a usable document rather than a 500.
    assert schema["type"] == "integer"


def test_a_nested_model_field_is_not_reported(caplog):
    """A `$ref` is the documented reason to fall back; warning there is noise."""
    app = Veloce(title="Nested", version="1.0.0")

    @app.get("/nested")
    async def nested(f: Nested = Query(group=True)) -> dict:
        return {"ok": True}

    with caplog.at_level(logging.WARNING, logger="veloce.contrib.openapi"):
        app.openapi()

    assert not [r for r in caplog.records if "grouped field" in r.getMessage()]
