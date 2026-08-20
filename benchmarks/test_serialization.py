"""Serialization benchmarks — encoding, JSON providers, signing.

Every response body goes through `jsonable_encoder` or a JSON response
class, so these are on the hot path of any API that returns data.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import uuid

from pydantic import BaseModel

from veloce import (
    JSONResponse,
    ORJSONResponse,
    PlainTextResponse,
    Response,
    Signer,
    jsonable_encoder,
)


class Address(BaseModel):
    street: str
    city: str
    zip_code: str


class User(BaseModel):
    id: int
    name: str
    email: str
    active: bool
    created_at: datetime.datetime
    address: Address
    roles: list[str]


@dataclasses.dataclass
class Point:
    x: float
    y: float
    label: str


USER = User(
    id=7,
    name="Ada Lovelace",
    email="ada@example.com",
    active=True,
    created_at=datetime.datetime(2024, 1, 15, 12, 30, 45),
    address=Address(street="12 Analytical Way", city="London", zip_code="EC1A 1BB"),
    roles=["admin", "engineer", "reviewer"],
)

USERS = [USER.model_copy(update={"id": i}) for i in range(50)]

# A payload mixing the types the encoder has dedicated branches for:
# UUID, datetime/date/time, Decimal, set, bytes, and a dataclass.
MIXED_PAYLOAD = {
    "uid": uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
    "when": datetime.datetime(2024, 6, 1, 8, 0, 0),
    "day": datetime.date(2024, 6, 1),
    "clock": datetime.time(8, 0, 0),
    "amount": decimal.Decimal("1234.56"),
    "tags": {"alpha", "beta", "gamma"},
    "blob": b"binary-payload",
    "point": Point(x=1.5, y=2.5, label="origin"),
    "nested": {"a": [1, 2, 3], "b": {"c": None, "d": True}},
}

FLAT_DICT = {f"key_{i}": f"value_{i}" for i in range(100)}

NESTED_DICT = {
    "results": [
        {"id": i, "name": f"item-{i}", "meta": {"score": i * 1.5, "active": i % 2 == 0}}
        for i in range(50)
    ],
    "total": 50,
    "page": 1,
}


# ── jsonable_encoder ───────────────────────────────────────


def test_encode_pydantic_model(benchmark):
    """Encode one nested Pydantic model."""
    assert benchmark(jsonable_encoder, USER)["id"] == 7


def test_encode_pydantic_model_list(benchmark):
    """Encode a 50-item model list — the typical collection endpoint."""
    assert len(benchmark(jsonable_encoder, USERS)) == 50


def test_encode_mixed_types(benchmark):
    """Encode the stdlib types with dedicated encoder branches."""
    assert benchmark(jsonable_encoder, MIXED_PAYLOAD)["amount"] is not None


def test_encode_flat_dict(benchmark):
    """100 string keys — measures the plain-dict walk with no coercion."""
    assert len(benchmark(jsonable_encoder, FLAT_DICT)) == 100


def test_encode_nested_dict(benchmark):
    """Nested lists of dicts — the recursion path."""
    assert benchmark(jsonable_encoder, NESTED_DICT)["total"] == 50


# ── Response rendering ─────────────────────────────────────


def test_json_response_render(benchmark):
    """Build a `JSONResponse` and serialize its body."""
    response = benchmark(JSONResponse, NESTED_DICT)
    assert response.status_code == 200


def test_orjson_response_render(benchmark):
    """Same payload through the orjson-backed response class."""
    response = benchmark(ORJSONResponse, NESTED_DICT)
    assert response.status_code == 200


def test_plain_text_response(benchmark):
    """The cheapest response class — a floor for response construction."""
    response = benchmark(PlainTextResponse, "Hello, World!")
    assert response.status_code == 200


def test_response_encode_wire_bytes(benchmark):
    """Serialize a response to its HTTP/1.1 wire form."""
    response = Response(
        body=b'{"message":"Hello, World!"}',
        content_type="application/json",
        headers={"x-request-id": "abc-123", "cache-control": "no-store"},
    )
    response.set_cookie("session", "opaque-session-value", httponly=True, samesite="lax")

    def encode_fresh() -> bytes:
        # `encode` memoizes into `_encoded`; drop the cache so every
        # iteration measures the encode rather than a dict lookup.
        response._encoded = None
        return response.encode()

    assert benchmark(encode_fresh).startswith(b"HTTP/1.1 200")


# ── Signing (sessions, CSRF, reset tokens) ─────────────────

SIGNER = Signer(secret="benchmark-secret-key", salt="benchmark")
SESSION_DATA = {"user_id": 42, "roles": ["admin"], "csrf": "token-value"}
SIGNED_TOKEN = SIGNER.dumps(SESSION_DATA)


def test_signer_dumps(benchmark):
    """Sign a session payload — runs on every response that sets a session."""
    assert benchmark(SIGNER.dumps, SESSION_DATA)


def test_signer_loads(benchmark):
    """Verify a signed session cookie — runs on every authenticated request."""
    assert benchmark(SIGNER.loads, SIGNED_TOKEN) == SESSION_DATA
