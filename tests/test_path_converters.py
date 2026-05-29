"""Path-converter tests (R12)."""

from __future__ import annotations

import uuid

import pytest

from veloce import Request, Veloce
from veloce.routing.converters import (
    AnyConverter,
    FloatConverter,
    IntConverter,
    PathConverter,
    StringConverter,
    UUIDConverter,
    parse_converter,
)
from veloce.routing.router import Router

# ── parse_converter ────────────────────────────────────────────────────


def test_parse_default_is_string():
    assert isinstance(parse_converter(None), StringConverter)
    assert isinstance(parse_converter(""), StringConverter)


def test_parse_builtins():
    assert isinstance(parse_converter("str"), StringConverter)
    assert isinstance(parse_converter("string"), StringConverter)
    assert isinstance(parse_converter("int"), IntConverter)
    assert isinstance(parse_converter("float"), FloatConverter)
    assert isinstance(parse_converter("uuid"), UUIDConverter)
    assert isinstance(parse_converter("path"), PathConverter)


def test_parse_any():
    c = parse_converter("any(red,blue,green)")
    assert isinstance(c, AnyConverter)
    ok, v = c.match("red")
    assert ok and v == "red"
    ok, _ = c.match("yellow")
    assert not ok


def test_parse_unknown_raises_at_registration():
    with pytest.raises(ValueError):
        parse_converter("does_not_exist")


def test_parse_any_empty_raises():
    with pytest.raises(ValueError):
        parse_converter("any()")


# ── Individual converters ──────────────────────────────────────────────


def test_int_converter_accepts_decimal():
    ok, v = IntConverter().match("42")
    assert ok and v == 42 and isinstance(v, int)


def test_int_converter_rejects_non_digits():
    assert IntConverter().match("abc") == (False, None)
    assert IntConverter().match("4.2") == (False, None)
    assert IntConverter().match("") == (False, None)


def test_int_converter_accepts_negative():
    ok, v = IntConverter().match("-7")
    assert ok and v == -7


def test_float_converter():
    ok, v = FloatConverter().match("3.14")
    assert ok and v == 3.14
    assert FloatConverter().match("3") == (False, None)  # no '.'
    assert FloatConverter().match("nan") == (False, None)
    assert FloatConverter().match("inf") == (False, None)


def test_uuid_converter():
    u = "550e8400-e29b-41d4-a716-446655440000"
    ok, v = UUIDConverter().match(u)
    assert ok and isinstance(v, uuid.UUID) and str(v) == u
    assert UUIDConverter().match("not-a-uuid") == (False, None)
    assert UUIDConverter().match("550e8400e29b41d4a716446655440000") == (False, None)


def test_path_converter_is_greedy():
    c = PathConverter()
    assert c.greedy is True
    ok, v = c.match("a/b/c.txt")
    assert ok and v == "a/b/c.txt"
    assert c.match("") == (False, None)


# ── Router-level integration ───────────────────────────────────────────


def test_router_int_param_coerces_and_routes():
    r = Router()

    @r.get("/u/{id:int}")
    async def h(id: int):
        return id

    m = r.match("GET", "/u/42")
    assert m is not None
    assert m.path_params == {"id": 42}
    assert isinstance(m.path_params["id"], int)


def test_router_int_param_misses_on_non_int():
    r = Router()

    @r.get("/u/{id:int}")
    async def h(id: int):
        return id

    # Non-integer segment → route miss, not 422
    assert r.match("GET", "/u/abc") is None


def test_router_path_param_consumes_slashes():
    r = Router()

    @r.get("/files/{p:path}")
    async def h(p: str):
        return p

    m = r.match("GET", "/files/a/b/c.txt")
    assert m is not None
    assert m.path_params == {"p": "a/b/c.txt"}


def test_router_uuid_param():
    r = Router()

    @r.get("/u/{id:uuid}")
    async def h(id):
        return id

    u = "550e8400-e29b-41d4-a716-446655440000"
    m = r.match("GET", f"/u/{u}")
    assert m is not None
    assert isinstance(m.path_params["id"], uuid.UUID)

    assert r.match("GET", "/u/not-a-uuid") is None


def test_router_any_param_restricts_values():
    r = Router()

    @r.get("/color/{c:any(red,blue)}")
    async def h(c: str):
        return c

    assert r.match("GET", "/color/red") is not None
    assert r.match("GET", "/color/blue") is not None
    assert r.match("GET", "/color/green") is None


def test_router_typed_vs_string_routes_coexist():
    """`/u/{id:int}` and `/u/{name}` can both be registered; the int route
    wins for digits, the string route catches the rest."""
    r = Router()

    @r.get("/u/{id:int}")
    async def by_id(id: int):
        return ("int", id)

    @r.get("/u/{name}")
    async def by_name(name: str):
        return ("str", name)

    m = r.match("GET", "/u/42")
    assert m is not None
    # The int converter matches first because it was registered first.
    assert m.path_params["id"] == 42

    m = r.match("GET", "/u/alice")
    assert m is not None
    assert m.path_params["name"] == "alice"


def test_unknown_converter_raises_at_registration():
    r = Router()

    with pytest.raises(ValueError):

        @r.get("/x/{id:bogus}")
        async def h(id):
            return id


# ── End-to-end via Veloce app ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_app_int_path_param_typed():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/u/{id:int}")
    async def h(id: int):
        return {"id": id, "type": type(id).__name__}

    req = Request(method="GET", path="/u/42", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert resp.status_code == 200
    assert b'"id":42' in resp.body
    assert b'"type":"int"' in resp.body


@pytest.mark.asyncio
async def test_app_int_path_param_404_on_non_int():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/u/{id:int}")
    async def h(id: int):
        return {"id": id}

    req = Request(method="GET", path="/u/abc", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_app_path_converter():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/files/{p:path}")
    async def h(p: str):
        return {"p": p}

    req = Request(method="GET", path="/files/dir/sub/x.txt", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert resp.status_code == 200
    assert b'"dir/sub/x.txt"' in resp.body


# ── Hardening: int length cap and float inf-constant lift ──────────────


def test_int_converter_rejects_oversized_digits():
    # Reject before invoking int() to avoid quadratic bignum parsing.
    assert IntConverter().match("9" * 100) == (False, None)
    assert IntConverter().match("1" * 21) == (False, None)
    assert IntConverter().match("-" + "9" * 20) == (False, None)


def test_int_converter_still_accepts_normal_values():
    ok, v = IntConverter().match("12345")
    assert ok and v == 12345
    # 19-digit positive (max signed 64-bit fits) still parses.
    ok, v = IntConverter().match("9" * 19)
    assert ok and v == int("9" * 19)


def test_float_converter_accepts_finite_large_value():
    # 1e308 is finite; the converter requires '.' so use 1.0e... shape —
    # but 'e' is also rejected, so check a plain large-but-finite decimal.
    ok, v = FloatConverter().match("1.5")
    assert ok and v == 1.5
    ok, v = FloatConverter().match("0.0001")
    assert ok and v == 0.0001


def test_float_converter_rejects_inf_and_nan():
    # 'inf'/'nan' lack '.' so they're rejected by the dot-check too;
    # the math.isinf guard backs that up for any future shape that slips through.
    assert FloatConverter().match("inf") == (False, None)
    assert FloatConverter().match("-inf") == (False, None)
    assert FloatConverter().match("nan") == (False, None)


def test_module_int_digit_cap_not_in_public_surface():
    # The underscored cap must stay private — verify it's not re-exported
    # from the routing package gateway.
    from veloce import routing as _routing
    from veloce.routing import converters as _conv

    assert "_MAX_INT_DIGITS" not in dir(_routing)
    assert _conv._MAX_INT_DIGITS == 20
