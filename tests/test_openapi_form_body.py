"""OpenAPI requestBody emission for Form() and File() handler params."""

from __future__ import annotations

from pydantic import BaseModel

from veloce import File, Form, Veloce
from veloce.contrib.openapi import get_openapi_schema


def _operation(app: Veloce, path: str, method: str = "post") -> dict:
    return get_openapi_schema(app)["paths"][path][method]


def test_form_only_emits_urlencoded_request_body() -> None:
    app = Veloce()

    @app.post("/login")
    async def login(request, username: str = Form(), password: str = Form()):
        return {"ok": True}

    op = _operation(app, "/login")
    body = op["requestBody"]
    assert body["required"] is True
    assert "application/x-www-form-urlencoded" in body["content"]
    assert "multipart/form-data" not in body["content"]
    schema = body["content"]["application/x-www-form-urlencoded"]["schema"]
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"username", "password"}
    assert schema["properties"]["username"] == {"type": "string"}
    assert sorted(schema["required"]) == ["password", "username"]


def test_file_upload_promotes_to_multipart() -> None:
    app = Veloce()

    @app.post("/upload")
    async def upload(request, caption: str = Form(), upload=File()):
        return {"ok": True}

    op = _operation(app, "/upload")
    body = op["requestBody"]
    assert "multipart/form-data" in body["content"]
    assert "application/x-www-form-urlencoded" not in body["content"]
    schema = body["content"]["multipart/form-data"]["schema"]
    assert schema["properties"]["upload"] == {"type": "string", "format": "binary"}
    assert schema["properties"]["caption"] == {"type": "string"}
    assert sorted(schema["required"]) == ["caption", "upload"]


def test_optional_form_field_omitted_from_required() -> None:
    app = Veloce()

    @app.post("/profile")
    async def profile(request, name: str = Form(), nickname: str = Form(default="anon")):
        return {"ok": True}

    op = _operation(app, "/profile")
    schema = op["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]
    assert "nickname" in schema["properties"]
    assert schema["required"] == ["name"]


def test_integer_form_field_uses_python_type_schema() -> None:
    app = Veloce()

    @app.post("/measure")
    async def measure(request, count: int = Form()):
        return {"ok": True}

    schema = _operation(app, "/measure")["requestBody"]["content"][
        "application/x-www-form-urlencoded"
    ]["schema"]
    assert schema["properties"]["count"] == {"type": "integer"}


class _Item(BaseModel):
    name: str
    price: float


def test_pydantic_body_still_emits_json_request_body() -> None:
    app = Veloce()

    @app.post("/items")
    async def create_item(request, body: _Item):
        return {"ok": True}

    op = _operation(app, "/items")
    body = op["requestBody"]
    assert "application/json" in body["content"]
    assert "application/x-www-form-urlencoded" not in body["content"]
    assert "multipart/form-data" not in body["content"]
    assert body["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/_Item"}


def test_only_file_param_emits_multipart_with_binary() -> None:
    app = Veloce()

    @app.post("/avatar")
    async def avatar(request, file=File()):
        return {"ok": True}

    schema = _operation(app, "/avatar")["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert schema["properties"] == {"file": {"type": "string", "format": "binary"}}
    assert schema["required"] == ["file"]
