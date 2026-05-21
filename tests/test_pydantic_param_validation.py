"""F1 — full Pydantic validation for query / path / header / cookie params.

Body models always got Pydantic validation; these tests cover the richer
non-body annotations — `datetime`, `date`, `UUID`, `Decimal`, `Literal`,
`Enum` — both at request time and in the emitted OpenAPI schema.
"""

from __future__ import annotations

import datetime
import uuid
from decimal import Decimal
from enum import Enum
from typing import Literal

from veloce import Cookie, Header, Query, Request, Veloce
from veloce.contrib.openapi import get_openapi_schema


class Color(str, Enum):
    red = "red"
    green = "green"


def make_request(method="GET", path="/", headers=None, body=b"", query_string="") -> Request:
    return Request(
        method=method,
        path=path,
        query_string=query_string,
        headers=headers or {},
        body=body,
    )


# ── runtime coercion: query parameters ────────────────────────────────


async def test_datetime_query_param_is_coerced():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/when")
    async def when(created: datetime.datetime = Query()):
        return {"type": type(created).__name__, "year": created.year}

    resp = await app.handle_request(
        make_request(path="/when", query_string="created=2021-06-15T09:30:00")
    )
    assert resp.status_code == 200
    assert b'"datetime"' in resp.body
    assert b'"year":2021' in resp.body or b'"year": 2021' in resp.body


async def test_date_query_param_is_coerced():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/day")
    async def day(d: datetime.date = Query()):
        return {"type": type(d).__name__}

    resp = await app.handle_request(make_request(path="/day", query_string="d=2021-06-15"))
    assert resp.status_code == 200
    assert b'"date"' in resp.body


async def test_decimal_query_param_is_coerced():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/price")
    async def price(amount: Decimal = Query()):
        return {"type": type(amount).__name__, "doubled": str(amount * 2)}

    resp = await app.handle_request(make_request(path="/price", query_string="amount=2.50"))
    assert resp.status_code == 200
    assert b'"Decimal"' in resp.body
    assert b"5.00" in resp.body


async def test_invalid_datetime_query_param_returns_422():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/when")
    async def when(created: datetime.datetime = Query()):
        return {"ok": True}

    resp = await app.handle_request(make_request(path="/when", query_string="created=not-a-date"))
    assert resp.status_code == 422


# ── runtime coercion: path / header / cookie ──────────────────────────


async def test_uuid_path_param_is_coerced():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/users/{user_id}")
    async def user(user_id: uuid.UUID):
        return {"type": type(user_id).__name__}

    real = "12345678-1234-5678-1234-567812345678"
    resp = await app.handle_request(make_request(path=f"/users/{real}"))
    assert resp.status_code == 200
    assert b'"UUID"' in resp.body


async def test_invalid_uuid_path_param_returns_422():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/users/{user_id}")
    async def user(user_id: uuid.UUID):
        return {"ok": True}

    resp = await app.handle_request(make_request(path="/users/not-a-uuid"))
    assert resp.status_code == 422


async def test_datetime_header_param_is_coerced():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/h")
    async def h(x_when: datetime.datetime = Header()):
        return {"type": type(x_when).__name__}

    resp = await app.handle_request(
        make_request(path="/h", headers={"x-when": "2021-06-15T09:30:00"})
    )
    assert resp.status_code == 200
    assert b'"datetime"' in resp.body


async def test_date_cookie_param_is_coerced():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/c")
    async def c(since: datetime.date = Cookie()):
        return {"type": type(since).__name__}

    resp = await app.handle_request(make_request(path="/c", headers={"cookie": "since=2021-06-15"}))
    assert resp.status_code == 200
    assert b'"date"' in resp.body


# ── Literal parameters ────────────────────────────────────────────────


async def test_literal_query_param_accepts_member():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/sort")
    async def sort(order: Literal["asc", "desc"] = Query(default="asc")):
        return {"order": order}

    resp = await app.handle_request(make_request(path="/sort", query_string="order=desc"))
    assert resp.status_code == 200
    assert b'"desc"' in resp.body


async def test_literal_query_param_rejects_non_member():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/sort")
    async def sort(order: Literal["asc", "desc"] = Query(default="asc")):
        return {"order": order}

    resp = await app.handle_request(make_request(path="/sort", query_string="order=sideways"))
    assert resp.status_code == 422


# ── the int / str fast path is untouched ──────────────────────────────


async def test_int_fast_path_still_works():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"type": type(item_id).__name__, "id": item_id}

    resp = await app.handle_request(make_request(path="/items/42"))
    assert resp.status_code == 200
    assert b'"int"' in resp.body


async def test_invalid_int_fast_path_still_422():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"ok": True}

    resp = await app.handle_request(make_request(path="/items/abc"))
    assert resp.status_code == 422


# ── OpenAPI parameter schemas are complete ────────────────────────────


def _param_schema(app: Veloce, path: str, name: str, method: str = "get") -> dict:
    schema = get_openapi_schema(app)
    params = schema["paths"][path][method].get("parameters", [])
    for p in params:
        if p["name"] == name:
            return p["schema"]
    raise AssertionError(f"parameter {name!r} not found in {path}")


def test_openapi_datetime_param_emits_date_time_format():
    app = Veloce()

    @app.get("/a")
    async def a(created: datetime.datetime = Query()):
        return {}

    sch = _param_schema(app, "/a", "created")
    assert sch["type"] == "string"
    assert sch["format"] == "date-time"


def test_openapi_date_param_emits_date_format():
    app = Veloce()

    @app.get("/a")
    async def a(d: datetime.date = Query()):
        return {}

    assert _param_schema(app, "/a", "d")["format"] == "date"


def test_openapi_uuid_param_emits_uuid_format():
    app = Veloce()

    @app.get("/u/{user_id}")
    async def u(user_id: uuid.UUID):
        return {}

    assert _param_schema(app, "/u/{user_id}", "user_id")["format"] == "uuid"


def test_openapi_enum_param_emits_enum_values():
    app = Veloce()

    @app.get("/c")
    async def c(color: Color = Query(default=Color.red)):
        return {}

    sch = _param_schema(app, "/c", "color")
    assert sch["enum"] == ["red", "green"]
    assert sch["type"] == "string"


def test_openapi_literal_param_emits_enum_values():
    app = Veloce()

    @app.get("/s")
    async def s(order: Literal["asc", "desc"] = Query(default="asc")):
        return {}

    sch = _param_schema(app, "/s", "order")
    assert sch["enum"] == ["asc", "desc"]
    assert sch["type"] == "string"


def test_openapi_int_param_still_integer():
    app = Veloce()

    @app.get("/i")
    async def i(n: int = Query(default=0)):
        return {}

    assert _param_schema(app, "/i", "n")["type"] == "integer"


def test_openapi_optional_datetime_param_keeps_format():
    """An `Optional[datetime]` parameter must still emit `date-time`."""
    app = Veloce()

    @app.get("/o")
    async def o(created: datetime.datetime | None = Query(default=None)):
        return {}

    sch = _param_schema(app, "/o", "created")
    assert sch["format"] == "date-time"


# ── non-string Literal parameters at runtime ──────────────────────────


async def test_int_literal_query_param_accepts_member():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/lvl")
    async def lvl(level: Literal[1, 2, 3] = Query(default=1)):
        return {"level": level, "type": type(level).__name__}

    resp = await app.handle_request(make_request(path="/lvl", query_string="level=2"))
    assert resp.status_code == 200
    assert b'"int"' in resp.body
    assert b'"level":2' in resp.body or b'"level": 2' in resp.body


async def test_int_literal_query_param_rejects_non_member():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/lvl")
    async def lvl(level: Literal[1, 2, 3] = Query(default=1)):
        return {"level": level}

    resp = await app.handle_request(make_request(path="/lvl", query_string="level=9"))
    assert resp.status_code == 422


async def test_bool_literal_query_param_accepts_member():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/flag")
    async def flag(on: Literal[True, False] = Query(default=False)):
        return {"on": on, "type": type(on).__name__}

    resp = await app.handle_request(make_request(path="/flag", query_string="on=true"))
    assert resp.status_code == 200
    assert b'"bool"' in resp.body
    assert b'"on":true' in resp.body or b'"on": true' in resp.body


# ── Decimal numeric constraints are enforced ──────────────────────────


async def test_decimal_param_ge_constraint_enforced():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/p")
    async def p(amount: Decimal = Query(ge=0)):
        return {"amount": str(amount)}

    ok = await app.handle_request(make_request(path="/p", query_string="amount=5"))
    assert ok.status_code == 200
    bad = await app.handle_request(make_request(path="/p", query_string="amount=-1"))
    assert bad.status_code == 422


async def test_decimal_param_multiple_of_constraint_enforced():
    """A `Decimal` `multiple_of` must not raise — both operands coerce."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/m")
    async def m(amount: Decimal = Query(multiple_of=Decimal("0.5"))):
        return {"amount": str(amount)}

    ok = await app.handle_request(make_request(path="/m", query_string="amount=1.5"))
    assert ok.status_code == 200
    bad = await app.handle_request(make_request(path="/m", query_string="amount=1.7"))
    assert bad.status_code == 422


# ── Literal of Enum members ───────────────────────────────────────────


async def test_str_enum_literal_query_param_accepts_member():
    """`Literal[StrEnum.member, ...]` matches the plain request string."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/co")
    async def co(c: Literal[Color.red, Color.green] = Query(default=Color.red)):
        return {"value": c.value}

    resp = await app.handle_request(make_request(path="/co", query_string="c=green"))
    assert resp.status_code == 200
    assert b'"green"' in resp.body


async def test_str_enum_literal_query_param_rejects_non_member():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/co")
    async def co(c: Literal[Color.red, Color.green] = Query(default=Color.red)):
        return {"value": c.value}

    resp = await app.handle_request(make_request(path="/co", query_string="c=purple"))
    assert resp.status_code == 422
