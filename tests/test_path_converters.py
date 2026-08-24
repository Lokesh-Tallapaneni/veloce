"""Path-converter tests (R12)."""

from __future__ import annotations

import re
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


def test_unknown_bareword_converter_raises_at_registration():
    # A bare-word converter spec outside the built-in set (a likely typo, e.g.
    # `{id:bogus}`) still fails loudly at registration rather than silently
    # becoming a literal-match route. Only a spec that looks like a regex
    # (`{id:[0-9]+}`) is taken as a raw-regex converter.
    r = Router()

    with pytest.raises(ValueError):

        @r.get("/x/{id:bogus}")
        async def h(id):
            return id


def test_raw_regex_converter_is_supported():
    # A spec carrying regex metacharacters is taken verbatim as the pattern.
    r = Router()

    @r.get("/x/{id:[0-9]+}")
    async def h(id):
        return id

    assert r.match("GET", "/x/42") is not None
    assert r.match("GET", "/x/abc") is None


def test_invalid_raw_regex_pattern_raises_at_registration():
    # A spec that is not a valid regex still fails loudly at registration,
    # when the pattern is compiled.
    r = Router()

    with pytest.raises(re.error):

        @r.get("/x/{id:[}")
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


# ── Temporal / decimal converters ───────────────────────────────────────

import datetime as _dt  # noqa: E402
import decimal as _dec  # noqa: E402

from veloce.routing.converters import (  # noqa: E402
    DateConverter,
    DateTimeConverter,
    DecimalConverter,
    TimeConverter,
    TimeDeltaConverter,
    extract_regex_converters,
)


def test_date_converter():
    c = DateConverter()
    ok, val = c.match("2024-01-15")
    assert ok and val == _dt.date(2024, 1, 15)
    assert c.match("2024-13-01") == (False, None)
    assert c.match("notadate") == (False, None)
    assert c.match("") == (False, None)


def test_datetime_converter():
    c = DateTimeConverter()
    ok, val = c.match("2024-01-15T12:30:00")
    assert ok and val == _dt.datetime(2024, 1, 15, 12, 30, 0)
    ok2, val2 = c.match("2024-01-15T12:30:00Z")
    assert ok2 and val2.tzinfo is not None
    assert c.match("garbage") == (False, None)


def test_time_converter():
    c = TimeConverter()
    ok, val = c.match("12:30")
    assert ok and val == _dt.time(12, 30)
    ok2, val2 = c.match("12:30:45.500")
    assert ok2 and val2 == _dt.time(12, 30, 45, 500000)
    assert c.match("25:00") == (False, None)


def test_timedelta_converter_strict():
    c = TimeDeltaConverter()
    ok, val = c.match("P1DT2H")
    assert ok and val == _dt.timedelta(days=1, hours=2)
    # Bare number rejected (strict vs Litestar).
    assert c.match("60") == (False, None)
    assert c.match("") == (False, None)


def test_timedelta_converter_accepts_str_repr():
    # Python's `str(timedelta)` form must round-trip (see url_for test below).
    c = TimeDeltaConverter()
    ok, val = c.match("1:00:00")
    assert ok and val == _dt.timedelta(hours=1)
    ok2, val2 = c.match("1 day, 2:00:00")
    assert ok2 and val2 == _dt.timedelta(days=1, hours=2)
    ok3, val3 = c.match("2 days, 3:04:05.500000")
    assert ok3 and val3 == _dt.timedelta(days=2, hours=3, minutes=4, seconds=5.5)
    # Negative timedelta repr (`-1 day, 23:00:00` == -1 hour) round-trips.
    neg = _dt.timedelta(hours=-1)
    ok4, val4 = c.match(str(neg))
    assert ok4 and val4 == neg


def test_timedelta_url_for_roundtrip():
    # url_for reverse-validates via converter.match(str(value)); a real
    # timedelta must build a URL that matches back to the same value.
    app = Veloce(openapi_url=None)

    @app.get("/wait/{delay:timedelta}", name="wait")
    async def h(request, delay):
        return {"delay": str(delay)}

    delay = _dt.timedelta(hours=1)
    url = app.url_for("wait", delay=delay)
    assert url == "/wait/1:00:00"
    m = app.match("GET", url)
    assert m is not None
    assert m.path_params["delay"] == delay


def test_decimal_converter():
    c = DecimalConverter()
    ok, val = c.match("3.14")
    assert ok and val == _dec.Decimal("3.14")
    ok2, val2 = c.match("-42")
    assert ok2 and val2 == _dec.Decimal("-42")
    assert c.match("nan") == (False, None)
    assert c.match("1e5") == (False, None)
    assert c.match("abc") == (False, None)
    assert c.match("9" * 50) == (False, None)


def test_parse_converter_temporal():
    assert isinstance(parse_converter("date"), DateConverter)
    assert isinstance(parse_converter("datetime"), DateTimeConverter)
    assert isinstance(parse_converter("time"), TimeConverter)
    assert isinstance(parse_converter("timedelta"), TimeDeltaConverter)
    assert isinstance(parse_converter("decimal"), DecimalConverter)


def test_router_date_param():
    r = Router()

    @r.get("/d/{when:date}")
    async def h(when):
        return when

    m = r.match("GET", "/d/2024-01-15")
    assert m is not None
    assert m.path_params["when"] == _dt.date(2024, 1, 15)
    assert r.match("GET", "/d/2024-13-99") is None


def test_regex_fallback_date_coercion():
    # A partial-segment placeholder forces the regex path.
    convs = extract_regex_converters("/v{ver:int}/d/{when:date}")
    assert isinstance(convs["when"], DateConverter)


def test_app_date_route_e2e():
    app = Veloce(openapi_url=None)

    @app.get("/d/{when:date}")
    async def h(request, when):
        return {"when": when.isoformat()}

    client = app.test_client()
    r = client.get("/d/2024-01-15")
    assert r.status_code == 200
    assert r.json()["when"] == "2024-01-15"
    assert client.get("/d/bad").status_code == 404


# ── A fragment must be a superset of its converter ───────────────────
#
# The regex fallback's fragments exist to pre-filter a segment before the
# converter re-validates it, and a comment beside them said they "stay
# permissive" for exactly that reason. `float` was not: `-?\d+\.\d+` is stricter
# than `FloatConverter.match`, so `+1.5`, `.5` and `5.` were rejected before the
# converter was consulted. The same route matched on the radix tree and 404'd on
# the regex fallback, so moving a route to a shape the tree cannot express -
# any partial-segment placeholder - silently narrowed what it accepted.

_FLOAT_SPELLINGS = ["1.5", "+1.5", "-1.5", ".5", "5."]


def _both_paths_app():
    from veloce import Veloce

    app = Veloce(openapi_url=None)

    @app.get("/t/{v:float}")
    async def radix(v: float):
        return {"v": v}

    # A partial-segment placeholder forces the regex fallback.
    @app.get("/r/pre{v:float}")
    async def regex(v: float):
        return {"v": v}

    return app


def _client():
    from veloce.testclient import TestClient

    return TestClient(_both_paths_app())


@pytest.mark.parametrize("value", _FLOAT_SPELLINGS)
def test_the_regex_fallback_accepts_every_float_the_tree_does(value):
    """The defect: three legal spellings 404'd on the regex path alone."""
    client = _client()
    assert client.get(f"/t/{value}").status_code == 200, value
    assert client.get(f"/r/pre{value}").status_code == 200, value


@pytest.mark.parametrize("value", _FLOAT_SPELLINGS)
def test_both_paths_coerce_to_the_same_value(value):
    client = _client()
    assert client.get(f"/t/{value}").json() == client.get(f"/r/pre{value}").json()


@pytest.mark.parametrize("value", ["1.5e3", "abc", "nan", "inf"])
def test_what_the_converter_rejects_is_rejected_on_both_paths(value):
    """Widening the fragment must not accept what the converter refuses."""
    client = _client()
    assert client.get(f"/t/{value}").status_code == 404, value
    assert client.get(f"/r/pre{value}").status_code == 404, value


def test_a_float_placeholder_does_not_swallow_an_int_route():
    """The dot stays required, so `123` is still an int and not a float."""
    from veloce import Veloce
    from veloce.testclient import TestClient

    app = Veloce(openapi_url=None)

    @app.get("/x/{v:float}")
    async def as_float(v: float):
        return {"kind": "float"}

    @app.get("/y/{v:int}")
    async def as_int(v: int):
        return {"kind": "int"}

    client = TestClient(app)
    assert client.get("/y/123").json() == {"kind": "int"}
    assert client.get("/x/123").status_code == 404
