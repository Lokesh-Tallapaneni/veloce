"""Tests for constrained route syntax - parametrized path converters.

`{n:int(min=1,max=100)}`, `{code:str(length=2)}`, `{x:float(min=0.0)}` bound
the segment during matching, so a violation is a route miss (404), not a
handler-layer error. Bounds participate on both the radix fast path and the
regex fallback, and `url_for` rejects an out-of-bounds value.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.routing.converters import parse_converter
from veloce.testclient import TestClient

# -- parse_converter unit coverage -----------------------------------


def test_int_min_max_parsing_and_matching():
    conv = parse_converter("int(min=1,max=100)")
    assert conv.match("50") == (True, 50)
    assert conv.match("0") == (False, None)
    assert conv.match("101") == (False, None)
    # signed by default, but a negative is below the min so it misses
    assert conv.match("-5") == (False, None)


def test_int_min_only_allows_large_values():
    conv = parse_converter("int(min=1)")
    assert conv.match("1") == (True, 1)
    assert conv.match("0") == (False, None)
    assert conv.match("999999") == (True, 999999)


def test_int_signed_false_rejects_negative():
    conv = parse_converter("int(signed=False)")
    assert conv.match("5") == (True, 5)
    assert conv.match("-5") == (False, None)


def test_int_zero_arg_keeps_legacy_signed_behavior():
    conv = parse_converter("int")
    assert conv.match("-5") == (True, -5)
    assert conv.match("5") == (True, 5)


def test_str_exact_length():
    conv = parse_converter("str(length=2)")
    assert conv.match("US") == (True, "US")
    assert conv.match("USA") == (False, None)
    assert conv.match("U") == (False, None)


def test_str_min_max_length():
    conv = parse_converter("str(minlength=3,maxlength=5)")
    assert conv.match("abc") == (True, "abc")
    assert conv.match("abcde") == (True, "abcde")
    assert conv.match("ab") == (False, None)
    assert conv.match("abcdef") == (False, None)


def test_float_bounds():
    conv = parse_converter("float(min=0.0,max=1.0)")
    assert conv.match("0.5") == (True, 0.5)
    assert conv.match("1.5") == (False, None)
    assert conv.match("-0.5") == (False, None)


def test_float_signed_false_rejects_negative():
    conv = parse_converter("float(signed=False)")
    assert conv.match("-0.5") == (False, None)
    assert conv.match("0.5") == (True, 0.5)


@pytest.mark.parametrize(
    "spec",
    [
        "int(min=5,max=1)",
        "str(length=0)",
        "str(length=2,minlength=1)",
        "int(bogus=1)",
        "date(min=1)",
        "int(min=)",
        "int(=1)",
        "int(min=1,min=2)",
    ],
)
def test_invalid_specs_raise_value_error(spec):
    with pytest.raises(ValueError):
        parse_converter(spec)


# -- end-to-end routing on the radix fast path -----------------------


def test_int_range_route_misses_out_of_bounds():
    app = Veloce()

    @app.get("/page/{n:int(min=1,max=100)}")
    async def page(n: int):
        return {"n": n}

    client = TestClient(app)
    assert client.get("/page/50").json() == {"n": 50}
    assert client.get("/page/1").status_code == 200
    assert client.get("/page/100").status_code == 200
    assert client.get("/page/0").status_code == 404
    assert client.get("/page/101").status_code == 404


def test_str_length_route():
    app = Veloce()

    @app.get("/country/{code:str(length=2)}")
    async def country(code: str):
        return {"code": code}

    client = TestClient(app)
    assert client.get("/country/US").json() == {"code": "US"}
    assert client.get("/country/USA").status_code == 404


def test_distinct_bounds_same_name_get_distinct_nodes():
    app = Veloce()

    @app.get("/a/{n:int(max=9)}")
    async def small(n: int):
        return {"slot": "small", "n": n}

    @app.get("/b/{n:int(min=10)}")
    async def big(n: int):
        return {"slot": "big", "n": n}

    client = TestClient(app)
    assert client.get("/a/5").json()["slot"] == "small"
    assert client.get("/a/50").status_code == 404
    assert client.get("/b/50").json()["slot"] == "big"
    assert client.get("/b/5").status_code == 404


# -- regex fallback path ---------------------------------------------


def test_bounds_enforced_on_regex_fallback():
    # A partial-segment placeholder forces the regex fallback; the bound must
    # still be enforced via the converter applied to the matched group.
    app = Veloce()

    @app.get("/v{n:int(min=1)}/info")
    async def vinfo(n: int):
        return {"v": n}

    client = TestClient(app)
    assert client.get("/v5/info").json() == {"v": 5}
    assert client.get("/v0/info").status_code == 404


# -- url_for reverse validation --------------------------------------


def test_url_for_accepts_in_bounds_value():
    app = Veloce()

    @app.get("/page/{n:int(min=1,max=100)}")
    async def page(n: int):
        return {"n": n}

    assert app.url_for("page", n=50) == "/page/50"


def test_url_for_rejects_out_of_bounds_value():
    from veloce import BuildError

    app = Veloce()

    @app.get("/page/{n:int(min=1,max=100)}")
    async def page(n: int):
        return {"n": n}

    with pytest.raises(BuildError):
        app.url_for("page", n=0)
