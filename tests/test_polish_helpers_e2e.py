"""End-to-end tests for polish-wave-2 helper changes (#45, #47, #51).

Fetches `/openapi.json` over the TestClient instead of calling
`get_openapi_schema(app)` directly, so the helper-split + form-body
emission + Authorization parsing fixes are validated through the same
ASGI pipeline real clients hit.
"""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Request, UploadFile, Veloce
from veloce.dependency import Security
from veloce.routing.params import File, Form, Header, Query
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


def test_openapi_helper_split_end_to_end_full_surface() -> None:
    bearer = HTTPBearer()
    app = Veloce(title="PolishWave2 API", version="9.9.9")

    @app.post(
        "/items/{item_id}",
        response_model=_ItemOut,
        status_code=201,
        tags=["x"],
    )
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

    doc = _fetch_openapi(app)

    assert doc["info"]["title"] == "PolishWave2 API"
    assert doc["info"]["version"] == "9.9.9"

    assert "/items/{item_id}" in doc["paths"]
    op = doc["paths"]["/items/{item_id}"]["post"]

    body_content = op["requestBody"]["content"]
    assert "application/json" in body_content
    schema_ref = body_content["application/json"]["schema"]
    assert schema_ref == {"$ref": "#/components/schemas/_CreateItem"}

    schemas = doc["components"]["schemas"]
    assert "_CreateItem" in schemas
    create_props = schemas["_CreateItem"]["properties"]
    assert create_props["name"]["type"] == "string"
    assert create_props["price"]["type"] == "number"

    assert "201" in op["responses"]
    assert "_ItemOut" in schemas

    sec_schemes = doc["components"]["securitySchemes"]
    assert sec_schemes["HTTPBearer"] == {"type": "http", "scheme": "bearer"}
    assert op["security"] == [{"HTTPBearer": []}]

    param_names = {p["name"] for p in op.get("parameters", [])}
    assert {"item_id", "q", "h"}.issubset(param_names)

    assert doc["webhooks"]["item.created"]["post"]["requestBody"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/_CreateItem"}


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


def test_authorization_digest_backslash_escaped_quote_decoded() -> None:
    app = Veloce(openapi_url=None)
    observed: dict = {}

    @app.get("/whoami")
    async def whoami(request: Request):
        auth = request.auth
        observed["type"] = auth.type if auth else None
        observed["params"] = dict(auth.params) if auth else {}
        return {"ok": True}

    header_value = (
        'Digest username="a\\"b", realm="test", nonce="abc", uri="/whoami", response="deadbeef"'
    )
    with TestClient(app) as client:
        resp = client.get("/whoami", headers={"Authorization": header_value})

    assert resp.status_code == 200
    assert observed["type"] == "digest"
    assert observed["params"]["username"] == 'a"b'
    assert observed["params"]["realm"] == "test"


def test_multipart_form_quoted_semicolon_in_part_name_end_to_end() -> None:
    app = Veloce(openapi_url=None)
    observed: dict = {}

    @app.post("/parts")
    async def parts(request: Request):
        form = await request.form()
        observed["value"] = form.get("x;y")
        observed["keys"] = list(form.keys())
        return {"ok": True}

    body = b'--BOUND\r\nContent-Disposition: form-data; name="x;y"\r\n\r\nhello\r\n--BOUND--\r\n'
    with TestClient(app) as client:
        resp = client.post(
            "/parts",
            content=body,
            headers={"Content-Type": "multipart/form-data; boundary=BOUND"},
        )

    assert resp.status_code == 200
    assert "x;y" in observed["keys"]
    assert observed["value"] == "hello"
