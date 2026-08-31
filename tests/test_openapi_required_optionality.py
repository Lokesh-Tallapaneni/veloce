"""OpenAPI `required` reflects what the resolver actually demands.

The schema is lowered from the same handler plan the resolver runs, so a value
the resolver binds to `None` when omitted is documented `required: false`, and a
value the route cannot be called without stays `required: true`.
"""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Form, Path, Query, Veloce
from veloce.http.datastructures import UploadFile


class _Item(BaseModel):
    x: int


def _op(app: Veloce, method: str, path: str) -> dict:
    return app.openapi()["paths"][path][method.lower()]


# ── path parameters are always required ───────────────────────────────


def test_optional_path_param_stays_required():
    app = Veloce(openapi_url=None)

    @app.get("/items/{item_id}")
    async def item(item_id: int | None = Path()):
        return {}

    param = _op(app, "get", "/items/{item_id}")["parameters"][0]
    assert param["in"] == "path"
    assert param["required"] is True


# ── optional query / header / cookie params are not required ──────────


def test_optional_query_param_without_default_is_not_required():
    app = Veloce(openapi_url=None)

    @app.get("/a")
    async def a(q: int | None):
        return {}

    with app.test_client() as client:
        assert client.get("/a").status_code == 200
    assert _op(app, "get", "/a")["parameters"][0]["required"] is False


def test_optional_query_marker_without_default_is_not_required():
    app = Veloce(openapi_url=None)

    @app.get("/b")
    async def b(q: int | None = Query()):
        return {}

    assert _op(app, "get", "/b")["parameters"][0]["required"] is False


def test_required_query_param_stays_required():
    app = Veloce(openapi_url=None)

    @app.get("/c")
    async def c(q: int):
        return {}

    assert _op(app, "get", "/c")["parameters"][0]["required"] is True


# ── request-body envelope reflects optionality ────────────────────────


def test_optional_annotated_json_body_stays_required():
    # `item: _Item | None` still requires a body: the resolver 422s when it is
    # absent (the model may be null in JSON, but a JSON document must be sent).
    app = Veloce(openapi_url=None)

    @app.post("/optbody")
    async def optbody(item: _Item | None):
        return {}

    with app.test_client() as client:
        assert client.post("/optbody").status_code == 422
    assert _op(app, "post", "/optbody")["requestBody"]["required"] is True


def test_required_json_body_stays_required():
    app = Veloce(openapi_url=None)

    @app.post("/reqbody")
    async def reqbody(item: _Item):
        return {}

    assert _op(app, "post", "/reqbody")["requestBody"]["required"] is True


def test_all_optional_form_body_is_not_required():
    app = Veloce(openapi_url=None)

    @app.post("/optform")
    async def optform(f: str | None = Form()):
        return {}

    with app.test_client() as client:
        assert client.post("/optform").status_code == 200
    assert _op(app, "post", "/optform")["requestBody"]["required"] is False


def test_form_body_with_a_required_field_is_required():
    app = Veloce(openapi_url=None)

    @app.post("/reqform")
    async def reqform(f: str = Form()):
        return {}

    assert _op(app, "post", "/reqform")["requestBody"]["required"] is True


# ── bare UploadFile honours its default ───────────────────────────────


def test_defaulted_upload_file_is_not_required():
    app = Veloce(openapi_url=None)

    @app.post("/optfile")
    async def optfile(up: UploadFile = None):
        return {}

    with app.test_client() as client:
        assert client.post("/optfile").status_code == 200
    body = _op(app, "post", "/optfile")["requestBody"]
    assert body["required"] is False
    assert "required" not in body["content"]["multipart/form-data"]["schema"]


def test_required_upload_file_stays_required():
    app = Veloce(openapi_url=None)

    @app.post("/reqfile")
    async def reqfile(up: UploadFile):
        return {}

    body = _op(app, "post", "/reqfile")["requestBody"]
    assert body["required"] is True
    assert body["content"]["multipart/form-data"]["schema"]["required"] == ["up"]
