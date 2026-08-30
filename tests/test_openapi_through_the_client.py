"""The OpenAPI document, fetched over the client rather than built directly.

Fetches `/openapi.json` over the TestClient instead of calling
`get_openapi_schema(app)` directly, so the helper-split + form-body
emission + Authorization parsing fixes are validated through the same
ASGI pipeline real clients hit.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from veloce import File, Form, Header, Query, Request, UploadFile, Veloce
from veloce.dependency import Security
from veloce.security.http import HTTPBearer
from veloce.testclient import TestClient


class _CreateItem(BaseModel):
    name: str
    price: float


class _ItemOut(BaseModel):
    id: int


def _fetch_openapi(app: Veloce) -> dict:
    with TestClient(app) as client:
        resp = client.get("/openapi.json")
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture(scope="module")
def full_surface() -> dict:
    """The document for one app exercising every emission concern at once.

    Built once and asserted on eight times. It used to be one test making
    fourteen assertions across those eight concerns, so the first failure hid
    the other seven and the report named the test rather than what broke.
    """
    bearer = HTTPBearer()
    app = Veloce(title="PolishWave2 API", version="9.9.9")

    @app.post("/items/{item_id}", response_model=_ItemOut, status_code=201, tags=["x"])
    async def create_item(
        request: Request,
        item_id: int,
        body: _CreateItem,
        q: str = Query(default="hi", max_length=10),
        h: str = Header(default="v"),
        tok=Security(bearer),
    ):
        return {"id": item_id}

    @app.webhooks.post("item.created")
    async def item_created(request: Request, body: _CreateItem):
        return {}

    return _fetch_openapi(app)


def _operation(doc: dict) -> dict:
    return doc["paths"]["/items/{item_id}"]["post"]


def test_the_info_block_carries_the_apps_title_and_version(full_surface: dict) -> None:
    assert full_surface["info"]["title"] == "PolishWave2 API"
    assert full_surface["info"]["version"] == "9.9.9"


def test_the_registered_path_is_described(full_surface: dict) -> None:
    assert "/items/{item_id}" in full_surface["paths"]


def test_the_request_body_refers_to_the_model(full_surface: dict) -> None:
    content = _operation(full_surface)["requestBody"]["content"]
    assert "application/json" in content
    assert content["application/json"]["schema"] == {"$ref": "#/components/schemas/_CreateItem"}


def test_the_body_model_is_a_component_with_typed_properties(full_surface: dict) -> None:
    schemas = full_surface["components"]["schemas"]
    assert "_CreateItem" in schemas
    properties = schemas["_CreateItem"]["properties"]
    assert properties["name"]["type"] == "string"
    assert properties["price"]["type"] == "number"


def test_the_declared_status_code_and_response_model_are_emitted(full_surface: dict) -> None:
    assert "201" in _operation(full_surface)["responses"]
    assert "_ItemOut" in full_surface["components"]["schemas"]


def test_the_security_scheme_is_declared_and_referenced(full_surface: dict) -> None:
    schemes = full_surface["components"]["securitySchemes"]
    assert schemes["HTTPBearer"] == {"type": "http", "scheme": "bearer"}
    assert _operation(full_surface)["security"] == [{"HTTPBearer": []}]


def test_every_parameter_source_reaches_the_document(full_surface: dict) -> None:
    """Path, query and header - three sources, one parameter list."""
    names = {p["name"] for p in _operation(full_surface).get("parameters", [])}
    assert {"item_id", "q", "h"}.issubset(names)


def test_a_webhook_body_refers_to_the_same_component(full_surface: dict) -> None:
    webhook = full_surface["webhooks"]["item.created"]["post"]
    assert webhook["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/_CreateItem"
    }


def test_openapi_multipart_request_body_end_to_end() -> None:
    app = Veloce()

    @app.post("/upload")
    async def upload(
        request: Request,
        file: UploadFile = File(...),
        name: str = Form(...),
    ):
        return {"ok": True}

    doc = _fetch_openapi(app)
    op = doc["paths"]["/upload"]["post"]
    content = op["requestBody"]["content"]
    assert "multipart/form-data" in content
    assert "application/x-www-form-urlencoded" not in content

    schema = content["multipart/form-data"]["schema"]
    assert schema["type"] == "object"
    assert schema["properties"]["file"] == {"type": "string", "format": "binary"}
    assert schema["properties"]["name"] == {"type": "string"}
    assert sorted(schema["required"]) == ["file", "name"]


def test_openapi_urlencoded_request_body_end_to_end() -> None:
    app = Veloce()

    @app.post("/echo")
    async def echo(request: Request, name: str = Form(...)):
        return {"ok": True}

    doc = _fetch_openapi(app)
    op = doc["paths"]["/echo"]["post"]
    content = op["requestBody"]["content"]
    assert "application/x-www-form-urlencoded" in content
    assert "multipart/form-data" not in content

    schema = content["application/x-www-form-urlencoded"]["schema"]
    assert schema["properties"]["name"] == {"type": "string"}
    assert schema["required"] == ["name"]
