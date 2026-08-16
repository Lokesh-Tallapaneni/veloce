"""`Body()` params are documented as a requestBody, embedded or not."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel

from veloce import Body, Veloce


class BodyModel(BaseModel):
    a: int


def _request_body(app: Veloce, path: str) -> dict | None:
    spec = app.test_client().get("/openapi.json").json()
    return spec["paths"][path]["post"].get("requestBody")


def test_embed_params_become_one_json_object_body():
    app = Veloce()

    @app.post("/items")
    async def add_item(
        title: Annotated[str | None, Body(None, embed=True)] = None,
        tags: Annotated[list[str] | None, Body(None, embed=True)] = None,
        is_task: Annotated[bool | None, Body(False, embed=True)] = False,
    ):
        return {}

    body = _request_body(app, "/items")
    assert body is not None, "an embedded Body() param must be documented"
    schema = body["content"]["application/json"]["schema"]
    assert schema["type"] == "object"
    assert schema["properties"]["title"] == {"type": "string"}
    assert schema["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert schema["properties"]["is_task"] == {"type": "boolean"}


def test_embed_body_is_required_only_when_a_field_is():
    app = Veloce()

    @app.post("/required")
    async def required(a: Annotated[str, Body(embed=True)]):
        return {}

    @app.post("/optional")
    async def optional(a: Annotated[str | None, Body(None, embed=True)] = None):
        return {}

    required_body = _request_body(app, "/required")
    assert required_body["required"] is True
    assert required_body["content"]["application/json"]["schema"]["required"] == ["a"]

    # Every field defaulted: the resolver accepts an absent body, so the
    # document must not claim one is required.
    optional_body = _request_body(app, "/optional")
    assert optional_body["required"] is False
    assert "required" not in optional_body["content"]["application/json"]["schema"]


def test_non_embedded_body_documents_the_whole_body():
    # Without `embed`, the resolver fills the param from the entire JSON body,
    # so the schema is the value's own schema rather than an object wrapper.
    app = Veloce()

    @app.post("/raw")
    async def raw(value: Annotated[str, Body()]):
        return {}

    body = _request_body(app, "/raw")
    assert body["required"] is True
    assert body["content"]["application/json"]["schema"] == {"type": "string"}


def test_documented_embed_body_matches_the_runtime():
    # The documented shape must be the one the resolver actually reads.
    app = Veloce()

    @app.post("/echo")
    async def echo(
        a: Annotated[str, Body(embed=True)],
        b: Annotated[int | None, Body(None, embed=True)] = None,
    ):
        return {"a": a, "b": b}

    client = app.test_client()
    assert client.post("/echo", json={"a": "x", "b": 2}).json() == {"a": "x", "b": 2}
    assert client.post("/echo", json={"a": "x"}).json() == {"a": "x", "b": None}
    # `a` is documented required; omitting it fails validation.
    assert client.post("/echo", json={}).status_code == 422


def test_model_body_still_takes_precedence():
    app = Veloce()

    @app.post("/model")
    async def with_model(m: BodyModel):
        return {}

    schema = _request_body(app, "/model")["content"]["application/json"]["schema"]
    assert "$ref" in schema
