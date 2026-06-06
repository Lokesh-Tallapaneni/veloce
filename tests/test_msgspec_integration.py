"""msgspec dual-backend - request validation, response serialization, OpenAPI.

A handler may type a body parameter or response as a `msgspec.Struct` instead of
a Pydantic `BaseModel`; it then decodes / encodes in C. Pydantic stays the
default and these tests assert full parity (validation errors, response shaping,
and OpenAPI schemas) plus that the Pydantic path is unchanged.
"""

from __future__ import annotations

import json
import re

import pytest
from pydantic import BaseModel

from veloce import ORJSONResponse, TestClient, Veloce

msgspec = pytest.importorskip("msgspec")


class Addr(msgspec.Struct):
    city: str
    zip: str


class User(msgspec.Struct):
    id: int
    name: str
    address: Addr


# Module-level so the string annotation (PEP 563) resolves via get_type_hints;
# a class defined inside a test function would not be in module globals.
class PUser(BaseModel):
    id: int
    name: str


def _app() -> Veloce:
    return Veloce(openapi_url=None)


_HEADERS = {"content-type": "application/json"}
_VALID = b'{"id":1,"name":"ada","address":{"city":"London","zip":"SW1"}}'


# ── Request-body validation ──────────────────────────────────────────


def test_struct_body_validates():
    app = _app()

    @app.post("/u")
    async def u(user: User):
        return {"id": user.id, "name": user.name, "city": user.address.city}

    r = TestClient(app).post("/u", content=_VALID, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"id": 1, "name": "ada", "city": "London"}


def test_struct_body_wrong_type():
    app = _app()

    @app.post("/u")
    async def u(user: User):
        return {"ok": True}

    r = TestClient(app).post(
        "/u",
        content=b'{"id":"notint","name":"x","address":{"city":"y","zip":"z"}}',
        headers=_HEADERS,
    )
    assert r.status_code == 422
    err = r.json()["detail"][0]
    assert err["loc"] == ["body"]
    # The field path lives inside the message (msgspec's format is not parsed).
    assert "id" in err["msg"]


def test_struct_body_malformed_json():
    app = _app()

    @app.post("/u")
    async def u(user: User):
        return {"ok": True}

    r = TestClient(app).post("/u", content=b"{not json", headers=_HEADERS)
    assert r.status_code == 422
    assert r.json()["detail"][0]["msg"] == "Invalid JSON body"


def test_struct_body_missing_required():
    app = _app()

    @app.post("/u")
    async def u(user: User):
        return {"ok": True}

    r = TestClient(app).post("/u", content=b'{"id":1}', headers=_HEADERS)
    assert r.status_code == 422


def test_struct_body_empty_optional():
    app = _app()

    @app.post("/u")
    async def u(user: User | None = None):
        return {"got": user is None}

    r = TestClient(app).post("/u", content=b"", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"got": True}


def test_struct_body_whitespace_optional():
    app = _app()

    @app.post("/u")
    async def u(user: User | None = None):
        return {"got": user is None}

    r = TestClient(app).post("/u", content=b"   \n  ", headers=_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"got": True}


def test_struct_body_null_required():
    # A literal `null` body is non-whitespace and is NOT treated as missing; it
    # decodes to a validation error (null is not a valid struct).
    app = _app()

    @app.post("/u")
    async def u(user: User):
        return {"ok": True}

    r = TestClient(app).post("/u", content=b"null", headers=_HEADERS)
    assert r.status_code == 422


# ── Response serialization ───────────────────────────────────────────


def test_struct_response_encodes():
    app = _app()

    @app.post("/u")
    async def u(user: User):
        return User(id=user.id, name=user.name.upper(), address=user.address)

    r = TestClient(app).post("/u", content=_VALID, headers=_HEADERS)
    assert r.json() == {"id": 1, "name": "ADA", "address": {"city": "London", "zip": "SW1"}}


def test_list_struct_response():
    app = _app()

    @app.get("/u")
    async def u():
        return [
            User(id=1, name="a", address=Addr(city="x", zip="1")),
            User(id=2, name="b", address=Addr(city="y", zip="2")),
        ]

    r = TestClient(app).get("/u")
    body = r.json()
    assert isinstance(body, list) and len(body) == 2
    assert body[0]["id"] == 1 and body[1]["name"] == "b"


def test_mixed_list_response():
    # A list mixing a struct and a dict encodes both (documents the allowed case).
    app = _app()

    @app.get("/u")
    async def u():
        return [User(id=1, name="a", address=Addr(city="x", zip="1")), {"plain": True}]

    r = TestClient(app).get("/u")
    body = r.json()
    assert body[0]["id"] == 1
    assert body[1] == {"plain": True}


def test_struct_tuple_status():
    # (struct, 201) -> status 201, body is the encoded struct, NOT a JSON array.
    app = _app()

    @app.post("/u")
    async def u(user: User):
        return User(id=user.id, name="created", address=user.address), 201

    r = TestClient(app).post("/u", content=_VALID, headers=_HEADERS)
    assert r.status_code == 201
    assert r.json()["name"] == "created"
    assert not isinstance(r.json(), list)


def test_struct_tuple_status_headers():
    app = _app()

    @app.post("/u")
    async def u(user: User):
        return User(id=user.id, name="x", address=user.address), 201, {"X-A": "b"}

    r = TestClient(app).post("/u", content=_VALID, headers=_HEADERS)
    assert r.status_code == 201
    assert r.headers["X-A"] == "b"
    assert r.json()["id"] == 1


def test_struct_with_custom_response_class():
    # A custom response_class renders the struct via to_builtins, not the
    # default msgspec-encoded Response.
    app = _app()

    @app.get("/u", response_class=ORJSONResponse)
    async def u():
        return User(id=7, name="z", address=Addr(city="c", zip="9"))

    r = TestClient(app).get("/u")
    assert r.status_code == 200
    assert r.json() == {"id": 7, "name": "z", "address": {"city": "c", "zip": "9"}}


def test_explicit_struct_response_model():
    # response_model=Struct must skip Pydantic's model_dump (which would crash)
    # and encode the struct.
    app = _app()

    @app.post("/u", response_model=User)
    async def u(user: User):
        return user

    r = TestClient(app).post("/u", content=_VALID, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["id"] == 1


# ── OpenAPI ──────────────────────────────────────────────────────────


def test_openapi_includes_struct_schema():
    app = Veloce()

    @app.post("/u", response_model=User)
    async def create(user: User) -> User:
        return user

    @app.get("/u", response_model=list[User])
    async def listing():
        return []

    spec = TestClient(app).get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    # Both the model and its nested struct get component schemas.
    assert "User" in schemas
    assert "Addr" in schemas
    # Request body refs the struct.
    rb = spec["paths"]["/u"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert rb == {"$ref": "#/components/schemas/User"}
    # Declared response_model schemas are emitted (the closed gap).
    post_resp = spec["paths"]["/u"]["post"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert post_resp == {"$ref": "#/components/schemas/User"}
    get_resp = spec["paths"]["/u"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert get_resp == {"type": "array", "items": {"$ref": "#/components/schemas/User"}}
    # Every $ref resolves - no dangling references.
    refs = set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(spec)))
    assert refs <= set(schemas), refs - set(schemas)


# ── Mixed app + Pydantic regression ──────────────────────────────────


def test_mixed_app_pydantic_and_msgspec():
    app = _app()

    @app.post("/msg")
    async def m(user: User):
        return {"backend": "msgspec", "id": user.id}

    @app.post("/pyd")
    async def p(user: PUser):
        return {"backend": "pydantic", "id": user.id}

    client = TestClient(app)
    rm = client.post("/msg", content=_VALID, headers=_HEADERS)
    rp = client.post("/pyd", content=b'{"id":9,"name":"x"}', headers=_HEADERS)
    assert rm.json() == {"backend": "msgspec", "id": 1}
    assert rp.json() == {"backend": "pydantic", "id": 9}


def test_pydantic_path_unchanged():
    # A Pydantic body endpoint behaves exactly as before: extra fields drop via
    # the model, a wrong type yields a structured per-field loc (not the
    # body-level loc msgspec uses).
    app = _app()

    @app.post("/p", response_model=PUser)
    async def p(user: PUser):
        return user

    client = TestClient(app)
    ok = client.post("/p", content=b'{"id":3,"name":"y","extra":"dropped"}', headers=_HEADERS)
    assert ok.status_code == 200
    assert ok.json() == {"id": 3, "name": "y"}
    bad = client.post("/p", content=b'{"id":"nope","name":"y"}', headers=_HEADERS)
    assert bad.status_code == 422
    # Pydantic keeps the structured field path in loc.
    assert bad.json()["detail"][0]["loc"][-1] == "id"


# ── No-msgspec behavior ──────────────────────────────────────────────


def test_backend_detection_basics():
    from veloce._model_backend import ModelBackend, backend_of, is_msgspec_struct, is_pydantic_model

    class PUser(BaseModel):
        x: int

    assert is_msgspec_struct(User) is True
    assert is_msgspec_struct(PUser) is False
    assert is_msgspec_struct(int) is False
    assert is_pydantic_model(PUser) is True
    assert backend_of(User) is ModelBackend.MSGSPEC
    assert backend_of(PUser) is ModelBackend.PYDANTIC
    assert backend_of(int) is ModelBackend.NONE


def test_is_msgspec_struct_safe_when_absent(monkeypatch):
    # Simulate msgspec not installed: the guard must short-circuit to False
    # without touching the (now None) struct reference.
    import veloce._model_backend as mb

    monkeypatch.setattr(mb, "_HAS_MSGSPEC", False)
    monkeypatch.setattr(mb, "_MSGSPEC_STRUCT", None)
    assert mb.is_msgspec_struct(User) is False
    assert mb.backend_of(User) is mb.ModelBackend.NONE
