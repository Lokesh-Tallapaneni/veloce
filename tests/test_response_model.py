"""response_model + dump-flag application tests (Q48)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from veloce import Veloce
from veloce.testclient import TestClient


class _UserPublic(BaseModel):
    """A public user view — explicitly does not include `password`."""

    id: int
    name: str


class _UserInternal(BaseModel):
    """The internal user view, including the secret. Returned by handlers
    that don't enforce a response_model."""

    id: int
    name: str
    password: str


def test_response_model_drops_undeclared_fields():
    """A handler returns the internal view; response_model strips the secret."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/me", response_model=_UserPublic)
    async def me():
        return {"id": 1, "name": "alice", "password": "secret123"}

    client = TestClient(app)
    resp = client.get("/me")
    body = resp.json()
    assert body == {"id": 1, "name": "alice"}
    assert "password" not in body


def test_response_model_validates_pydantic_return():
    """A handler may return a BaseModel instance; response_model still validates."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/me", response_model=_UserPublic)
    async def me():
        return _UserInternal(id=2, name="bob", password="hidden")

    client = TestClient(app)
    body = client.get("/me").json()
    assert body == {"id": 2, "name": "bob"}


def test_response_model_list_of_models():
    """response_model=list[Model] validates each element."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/users", response_model=list[_UserPublic])
    async def users():
        return [
            {"id": 1, "name": "a", "password": "x"},
            {"id": 2, "name": "b", "password": "y"},
        ]

    client = TestClient(app)
    body = client.get("/users").json()
    assert body == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_response_model_exclude_unset():
    """exclude_unset drops fields the handler didn't explicitly set."""
    app = Veloce(debug=True, openapi_url=None)

    class M(BaseModel):
        a: str = "default-a"
        b: str = "default-b"

    @app.get("/m", response_model=M, response_model_exclude_unset=True)
    async def m():
        return M(a="explicit-a")  # b is unset

    body = TestClient(app).get("/m").json()
    assert body == {"a": "explicit-a"}


def test_response_model_exclude_none():
    """exclude_none drops keys whose value is None."""
    app = Veloce(debug=True, openapi_url=None)

    class M(BaseModel):
        a: str
        b: str | None = None

    @app.get("/m", response_model=M, response_model_exclude_none=True)
    async def m():
        return M(a="x", b=None)

    body = TestClient(app).get("/m").json()
    assert body == {"a": "x"}


def test_response_model_by_alias():
    """by_alias serializes via Pydantic field aliases."""
    app = Veloce(debug=True, openapi_url=None)

    class M(BaseModel):
        user_id: int = Field(alias="userId")
        model_config = {"populate_by_name": True}

    @app.get("/m", response_model=M, response_model_by_alias=True)
    async def m():
        return M(user_id=42)

    body = TestClient(app).get("/m").json()
    assert body == {"userId": 42}


def test_response_model_include():
    """include={'name'} keeps only the listed fields."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/me", response_model=_UserPublic, response_model_include={"name"})
    async def me():
        return _UserPublic(id=1, name="alice")

    body = TestClient(app).get("/me").json()
    assert body == {"name": "alice"}


def test_response_model_exclude():
    """exclude={'id'} drops the listed fields."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/me", response_model=_UserPublic, response_model_exclude={"id"})
    async def me():
        return _UserPublic(id=1, name="alice")

    body = TestClient(app).get("/me").json()
    assert body == {"name": "alice"}


def test_response_model_not_applied_when_handler_returns_response():
    """If the handler returns a raw Response, response_model is bypassed —
    the user has taken explicit control of the wire format."""
    from veloce.http.response import JSONResponse

    app = Veloce(debug=True, openapi_url=None)

    @app.get("/me", response_model=_UserPublic)
    async def me():
        return JSONResponse({"id": 1, "name": "a", "password": "leak"})

    body = TestClient(app).get("/me").json()
    # password is NOT dropped — the user opted out by returning a Response.
    assert body == {"id": 1, "name": "a", "password": "leak"}


def test_no_response_model_is_a_passthrough():
    """Without response_model, the dict is serialized as-is."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/raw")
    async def raw():
        return {"x": 1, "y": 2}

    body = TestClient(app).get("/raw").json()
    assert body == {"x": 1, "y": 2}
