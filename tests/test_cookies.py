"""What `parse_cookie` and `dump_cookie` refuse, and why.

The validation half of the pair: CRLF, LF and NUL in a key, path, domain or
value; non-token and reserved names; and that `Response.set_cookie` propagates
the same refusals rather than validating separately.

What they *render and parse* when the input is valid is
`tests/test_cookie_helpers.py`. Both modules named `parse_cookie` and
`dump_cookie` in their docstrings and neither said which half it held.
"""

from __future__ import annotations

import pytest

from veloce import Request, Response, Veloce
from veloce.http.cookies import dump_cookie, parse_cookie
from veloce.http.datastructures import Cookies
from veloce.middleware.sessions import ServerSessionMiddleware, SessionMiddleware
from veloce.testclient import TestClient


def test_dump_cookie_basic():
    out = dump_cookie("session", "abc123")
    assert out.startswith("session=abc123")
    assert "Path=/" in out


def test_dump_cookie_with_all_attributes():
    out = dump_cookie(
        "session",
        "v",
        max_age=3600,
        path="/app",
        domain="example.com",
        secure=True,
        httponly=True,
        samesite="Lax",
    )
    assert "session=v" in out
    assert "Max-Age=3600" in out
    assert "Path=/app" in out
    assert "Domain=example.com" in out
    assert "Secure" in out
    assert "HttpOnly" in out
    assert "SameSite=Lax" in out


def test_dump_cookie_rejects_crlf_in_key():
    with pytest.raises(ValueError, match="cookie name"):
        dump_cookie("ab\r\ncd", "v")


def test_dump_cookie_rejects_lf_in_key():
    with pytest.raises(ValueError, match="cookie name"):
        dump_cookie("ab\nInjected: yes", "v")


def test_dump_cookie_rejects_lf_in_path():
    with pytest.raises(ValueError, match="cookie path"):
        dump_cookie("ab", "v", path="/x\nattack")


def test_dump_cookie_rejects_crlf_in_path():
    with pytest.raises(ValueError, match="cookie path"):
        dump_cookie("ab", "v", path="/x\r\nSet-Cookie: evil=1")


def test_dump_cookie_rejects_lf_in_domain():
    with pytest.raises(ValueError, match="cookie domain"):
        dump_cookie("ab", "v", domain="example.com\nInjected: yes")


def test_dump_cookie_rejects_nul_in_key():
    with pytest.raises(ValueError, match="cookie name"):
        dump_cookie("ab\x00cd", "v")


@pytest.mark.parametrize("bad", ["a b", "foo;bar", "foo=bar", 'foo"bar', ""])
def test_dump_cookie_rejects_non_token_name(bad):
    with pytest.raises(ValueError, match="cookie name"):
        dump_cookie(bad, "v")


@pytest.mark.parametrize("reserved", ["Path", "path", "Max-Age", "SameSite", "Secure"])
def test_dump_cookie_rejects_reserved_name(reserved):
    with pytest.raises(ValueError, match="reserved"):
        dump_cookie(reserved, "v")


@pytest.mark.parametrize("good", ["session", "__Host-session", "__Secure-id", "my.cookie_name-1"])
def test_dump_cookie_accepts_valid_token_names(good):
    out = dump_cookie(good, "abc")
    assert out.startswith(f"{good}=")


def test_set_cookie_propagates_name_validation():

    with pytest.raises(ValueError, match="cookie name"):
        Response().set_cookie("bad name", "v")


def test_dump_cookie_rejects_crlf_in_value():
    with pytest.raises(ValueError, match="cookie value"):
        dump_cookie("ab", "v\r\nattack")


def test_dump_cookie_rejects_lf_in_value():
    with pytest.raises(ValueError, match="cookie value"):
        dump_cookie("ab", "v\nSet-Cookie: evil=1")


def test_dump_cookie_rejects_crlf_in_samesite():
    with pytest.raises(ValueError, match="cookie samesite"):
        dump_cookie("ab", "v", samesite="Strict\r\nInjected")


def test_dump_cookie_rejects_unknown_samesite():
    with pytest.raises(ValueError, match="samesite must be"):
        dump_cookie("ab", "v", samesite="bogus")


def test_dump_cookie_samesite_case_insensitive():
    out = dump_cookie("ab", "v", samesite="strict")
    assert "SameSite=Strict" in out


def test_parse_cookie_round_trip():
    raw = dump_cookie("session", "hello world", path="/")
    name, _, attrs = raw.partition(";")
    parsed = parse_cookie(name)
    assert parsed == {"session": "hello world"}


# -- Multiple / merged Cookie headers (RFC 6265) ----------------------


def _req(headers):

    return Request(method="GET", path="/", query_string="", headers=headers, body=b"")


def test_two_cookie_headers_both_parse():
    req = _req([(b"cookie", b"a=1"), (b"cookie", b"b=2")])
    cookies = req.cookies
    assert cookies["a"] == "1"
    assert cookies["b"] == "2"


def test_single_combined_cookie_header():
    req = _req([(b"cookie", b"a=1; b=2")])
    cookies = req.cookies
    assert cookies["a"] == "1"
    assert cookies["b"] == "2"


def test_no_cookie_header_empty():
    req = _req([(b"host", b"x")])
    assert len(req.cookies) == 0


def test_duplicate_cookie_name_first_wins():
    req = _req([(b"cookie", b"a=1"), (b"cookie", b"a=2")])
    assert req.cookies["a"] == "1"


def test_cookies_cached():
    req = _req([(b"cookie", b"a=1")])
    first = req.cookies
    assert req.cookies is first


# ── `samesite` is normalised once, in the serialiser ─────────────────
#
# Three places fixed the value up on the way in: `dump_cookie` stripped and
# capitalised, `Response.set_cookie` turned a whitespace-only value into `None`,
# and `SessionMiddleware` carried a pre-capitalised `_samesite_cap` while its own
# delete path passed the raw string through. So a value one of them rejected
# reached the serialiser from one caller and not another: `samesite="  "` made
# the cookie-backed session raise on *every* response while the server-side one
# shipped a cookie with no `SameSite` at all.
#
# The whole rule lives in `dump_cookie` now, so there is no "on the way in" for
# the copies to disagree about.


@pytest.mark.parametrize("value", ["lax", "Lax", "LAX", " lax "])
def test_any_casing_or_padding_renders_the_canonical_attribute(value):

    response = Response(body=b"x")
    response.set_cookie("k", "v", samesite=value)
    assert "SameSite=Lax" in response.headers["Set-Cookie"]


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_a_blank_samesite_omits_the_attribute(value):

    response = Response(body=b"x")
    response.set_cookie("k", "v", samesite=value)
    assert "SameSite" not in response.headers["Set-Cookie"]


def test_an_unrecognised_samesite_is_still_refused():

    response = Response(body=b"x")
    with pytest.raises(ValueError, match="samesite"):
        response.set_cookie("k", "v", samesite="sometimes")


@pytest.mark.parametrize("value", ["  ", "lax", "STRICT", ""])
def test_both_session_backends_answer_a_samesite_the_same_way(value):
    """The defect: one raised on every response where the other stayed silent."""

    def render(middleware) -> str:
        app = Veloce(openapi_url=None)
        app.secret_key = "k"
        app.add_middleware(middleware)

        @app.get("/s")
        async def touch(request):
            request.session["u"] = 1
            return {"ok": True}

        response = TestClient(app).get("/s")
        assert response.status_code == 200
        cookie = response.headers.get("set-cookie", "")
        return next(
            (p.strip() for p in cookie.split(";") if p.strip().lower().startswith("samesite")),
            "",
        )

    assert render(SessionMiddleware(secret_key="k", samesite=value)) == render(
        ServerSessionMiddleware(samesite=value)
    )


def test_a_session_delete_cookie_agrees_with_its_write():
    """`_delete_cookie` passed the raw value while `_render_cookie` capitalised."""

    app = Veloce(openapi_url=None)
    app.secret_key = "k"
    app.add_middleware(SessionMiddleware(secret_key="k", samesite="STRICT"))

    @app.get("/in")
    async def sign_in(request):
        request.session["u"] = 1
        return {"ok": True}

    @app.get("/out")
    async def sign_out(request):
        request.session.clear()
        return {"ok": True}

    client = TestClient(app)
    assert "SameSite=Strict" in client.get("/in").headers["set-cookie"]
    cleared = client.get("/out").headers.get("set-cookie", "")
    assert "SameSite=STRICT" not in cleared


# ── Cookies MultiDict ──────────────────────────────────────────
#
# Moved here from `test_cookies_and_validation.py`, which bundled cookie parsing
# with validation-error hierarchy behaviour.


def test_cookies_parses_single_cookie():
    c = Cookies.from_cookie_header("session=abc123")
    assert c["session"] == "abc123"
    assert c.getlist("session") == ["abc123"]


def test_cookies_parses_multiple_distinct_cookies():
    c = Cookies.from_cookie_header("a=1; b=2; c=3")
    assert c["a"] == "1"
    assert c["b"] == "2"
    assert c["c"] == "3"


def test_cookies_first_wins_on_duplicate_names():
    """RFC 6265 section 5.4: duplicate names collapse to first occurrence."""
    c = Cookies.from_cookie_header("tag=x; tag=y; other=z")
    assert c.getlist("tag") == ["x"]
    assert c["tag"] == "x"
    assert c["other"] == "z"


def test_cookies_strips_whitespace():
    c = Cookies.from_cookie_header(" a=1 ;  b=2 ")
    assert c["a"] == "1"
    assert c["b"] == "2"


def test_cookies_skips_attributes_without_value():
    """Attributes like `Secure` or `HttpOnly` belong on Set-Cookie, not
    Cookie — but if they appear, skip them silently."""
    c = Cookies.from_cookie_header("a=1; Secure; b=2")
    assert c["a"] == "1"
    assert c["b"] == "2"
    assert c.getlist("Secure") == []


def test_cookies_getlist_missing_returns_empty():
    c = Cookies.from_cookie_header("a=1")
    assert c.getlist("missing") == []


def test_cookies_empty_header():
    c = Cookies.from_cookie_header("")
    assert len(c) == 0


def test_request_cookies_is_cookies_instance():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={"cookie": "x=1; y=2"},
        body=b"",
    )
    assert isinstance(req.cookies, Cookies)
    assert req.cookies["x"] == "1"
