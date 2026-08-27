"""response_model + dump-flag application tests (Q48)."""

from __future__ import annotations

from pydantic import BaseModel

from veloce import Veloce
from veloce.http.response import JSONResponse
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


# The five dump-flag behaviours that were here - `exclude_unset`,
# `exclude_none`, `by_alias`, `include`, `exclude` - are covered in
# `test_response_model_dump_kwargs.py`, which owns them: it also covers
# `exclude_defaults`, the combinations, an empty include collection, and that
# two routes do not share one precomputed mapping. Asserting a subset of that
# here a second time meant a change to the flags had two places to be noticed
# and one of them said less.
#
# What stays in this module is what is about `response_model` itself rather
# than about how the model is dumped.


def test_response_model_not_applied_when_handler_returns_response():
    """If the handler returns a raw Response, response_model is bypassed —
    the user has taken explicit control of the wire format."""

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


# ── list[Model] fast path (P-5) ──────────────────────────────────────


def test_response_model_list_of_target_model_instances():
    """`list[Model]` with handler-returned instances of the target model —
    the fast path dumps them directly without a re-validation round-trip."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/users", response_model=list[_UserPublic])
    async def users():
        return [_UserPublic(id=1, name="alice"), _UserPublic(id=2, name="bob")]

    body = TestClient(app).get("/users").json()
    assert body == [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]


def test_response_model_list_exclude_unset_per_element():
    """`exclude_unset` applies per element: the fast path keeps each
    instance's fields-set markers, which a validate round-trip would erase."""
    app = Veloce(debug=True, openapi_url=None)

    class M(BaseModel):
        a: str = "default-a"
        b: str = "default-b"

    @app.get("/items", response_model=list[M], response_model_exclude_unset=True)
    async def items():
        return [M(a="x"), M(b="y")]  # each leaves one field unset

    body = TestClient(app).get("/items").json()
    assert body == [{"a": "x"}, {"b": "y"}]
