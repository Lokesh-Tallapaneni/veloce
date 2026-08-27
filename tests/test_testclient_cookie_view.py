"""What a test asserts on a cookie is what the handler was given.

The test client had two cookie readers. The jar took the first `name=value`
segment of a `Set-Cookie` header verbatim, which is right for the jar — it
re-emits those bytes on the next request, the way a browser does. But
`client.cookies` and `response.cookies` are read through the same store, and
those are assertion surfaces: they were reporting the wire form.

So for any value carrying an escape — a space, a `;`, a `=`, a comma — the test
client disagreed with `iter_cookies`, the one cookie reader the server uses.
`client.cookies["pref"]` said `a%20b%3Bc=d` while the handler was given
`a b;c=d`, and a test asserting the value the handler sees failed against a
server that was sending exactly that.

The jar still stores the wire form. The read side decodes, through the same
helper `iter_cookies` uses, so the two cannot drift again.
"""

from __future__ import annotations

import pytest

from veloce import Response, SessionMiddleware, Veloce
from veloce.http.cookies import iter_cookies
from veloce.testclient import AsyncTestClient, TestClient, _decode_cookie_value

#: Values that survive a round trip but need encoding on the wire. Each is what
#: the handler set and what every read surface must report.
ROUND_TRIP_VALUES = [
    "plain",
    "a b",
    "a;b",
    "a=b",
    "a,b",
    "a%b",
    'a"b',
    "a b;c=d",
    "50%",
    "100%%",
    "été",
    "a\tb",
    "",
]


def _app() -> Veloce:
    app = Veloce(openapi_url=None)

    @app.get("/set")
    async def set_cookie(request):
        response = Response(body=b"{}", content_type="application/json")
        response.set_cookie("pref", request.query_params.get("v", ""))
        return response

    @app.get("/read")
    async def read(request) -> dict:
        return {"pref": request.cookies.get("pref")}

    return app


# ── the reported disagreement ────────────────────────────────────────


def test_the_client_reports_the_value_the_handler_receives():
    """The defect, exactly as reproduced: these two disagreed."""
    client = TestClient(_app())
    client.get("/set", params={"v": "a b;c=d"})
    assert client.cookies["pref"] == "a b;c=d"
    assert client.get("/read").json()["pref"] == "a b;c=d"


def test_the_response_reports_the_value_the_handler_receives():
    client = TestClient(_app())
    response = client.get("/set", params={"v": "a b;c=d"})
    assert response.cookies["pref"] == "a b;c=d"


def test_the_wire_header_is_still_encoded():
    """The fix must not change what goes over the wire."""
    client = TestClient(_app())
    response = client.get("/set", params={"v": "a b;c=d"})
    assert "pref=a%20b%3Bc=d" in response.headers["set-cookie"]


def test_the_jar_still_holds_the_wire_form():
    """It re-emits these bytes verbatim; decoding them would corrupt the send."""
    client = TestClient(_app())
    client.get("/set", params={"v": "a b;c=d"})
    assert client._cookies["pref"] == "a%20b%3Bc=d"


# ── every read surface agrees ────────────────────────────────────────


@pytest.mark.parametrize("value", ROUND_TRIP_VALUES)
def test_every_read_surface_agrees_with_the_handler(value):
    client = TestClient(_app())
    response = client.get("/set", params={"v": value})
    served = client.get("/read").json()["pref"]

    assert served == value
    assert client.cookies["pref"] == value
    assert client.cookies.get("pref") == value
    assert dict(client.cookies.items())["pref"] == value
    assert value in list(client.cookies.values())
    assert response.cookies["pref"] == value


def test_the_repr_shows_decoded_values():
    client = TestClient(_app())
    client.get("/set", params={"v": "a b"})
    assert "a b" in repr(client.cookies)
    assert "%20" not in repr(client.cookies)


def test_a_missing_key_still_raises():
    client = TestClient(_app())
    with pytest.raises(KeyError):
        client.cookies["nope"]


def test_a_missing_key_returns_the_default():
    client = TestClient(_app())
    assert client.cookies.get("nope") is None
    assert client.cookies.get("nope", "fallback") == "fallback"


def test_a_cookie_set_to_empty_is_found_not_defaulted():
    """`get` must distinguish an absent key from one holding an empty value."""
    client = TestClient(_app())
    client.get("/set", params={"v": ""})
    assert client.cookies.get("pref", "fallback") == ""


def test_names_and_membership_are_untouched():
    client = TestClient(_app())
    client.get("/set", params={"v": "a b"})
    assert "pref" in client.cookies
    assert list(client.cookies.keys()) == ["pref"]
    assert list(client.cookies) == ["pref"]
    assert len(client.cookies) == 1


# ── the two readers cannot drift ─────────────────────────────────────


@pytest.mark.parametrize("value", ROUND_TRIP_VALUES)
def test_the_client_view_matches_the_server_side_parser(value):
    """Both go through `_decode_cookie_value`, so this is structural."""
    client = TestClient(_app())
    client.get("/set", params={"v": value})
    wire = client._cookies["pref"]
    assert _decode_cookie_value(wire) == dict(iter_cookies(f"pref={wire}"))["pref"]
    assert client.cookies["pref"] == _decode_cookie_value(wire)


def test_iter_cookies_still_decodes_the_way_it_did():
    """The decode step was lifted out of it; its behaviour is unchanged."""
    assert dict(iter_cookies('a=1; b=x%20y; c="q"; d')) == {"a": "1", "b": "x y", "c": "q"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("  padded  ", "padded"),
        ("a%20b", "a b"),
        ('"quoted"', "quoted"),
        ('"a%20b"', "a b"),
        ("50%", "50%"),
        ("", ""),
    ],
)
def test_the_decoder_handles_each_shape(raw, expected):
    assert _decode_cookie_value(raw) == expected


# ── writes ───────────────────────────────────────────────────────────


def test_a_written_cookie_reaches_the_handler():
    client = TestClient(_app())
    client.cookies["pref"] = "written"
    assert client.get("/read").json()["pref"] == "written"


def test_a_written_cookie_reads_back_as_the_handler_sees_it():
    """A write is a wire value, so a read of it decodes - and both agree."""
    client = TestClient(_app())
    client.cookies["pref"] = "a%20b"
    assert client.cookies["pref"] == "a b"
    assert client.get("/read").json()["pref"] == "a b"


def test_update_and_clear_still_work():
    client = TestClient(_app())
    client.cookies.update({"a": "1", "b": "2"})
    assert client.cookies["a"] == "1"
    client.cookies.clear()
    assert len(client.cookies) == 0


# ── the header shapes the parser has to survive ──────────────────────


def _client_for(raw_header: str) -> TestClient:
    """An app that emits one literal `Set-Cookie` header."""
    app = Veloce(openapi_url=None)

    @app.get("/one")
    async def one():
        response = Response(body=b"{}", content_type="application/json")
        response.headers["Set-Cookie"] = raw_header
        return response

    return TestClient(app)


def test_several_set_cookie_headers_are_all_recorded():
    app = Veloce(openapi_url=None)

    @app.get("/multi")
    async def multi():
        response = Response(body=b"{}", content_type="application/json")
        response.set_cookie("a", "1")
        response.set_cookie("b", "x y")
        response.set_cookie("c", "3")
        return response

    client = TestClient(app)
    response = client.get("/multi")
    assert response.cookies == {"a": "1", "b": "x y", "c": "3"}
    assert client.cookies["b"] == "x y"
    assert dict(client.cookies.items()) == {"a": "1", "b": "x y", "c": "3"}


def test_an_attribute_only_first_segment_is_skipped():
    """A header whose leading segment carries no `=` is not a cookie."""
    client = _client_for("HttpOnly; a=1")
    response = client.get("/one")
    assert response.cookies == {}
    assert len(client.cookies) == 0


def test_only_the_first_segment_is_the_cookie():
    """`Expires` carries an `=` too; it must not become a cookie."""
    client = _client_for("a=1; Expires=Wed, 21 Oct 2099 07:28:00 GMT; Path=/")
    response = client.get("/one")
    assert response.cookies == {"a": "1"}
    assert dict(client.cookies.items()) == {"a": "1"}


def test_an_encoded_value_in_a_literal_header_is_decoded_on_read():
    client = _client_for("pref=a%20b%3Bc%3Dd; Path=/")
    response = client.get("/one")
    assert response.cookies == {"pref": "a b;c=d"}
    assert client.cookies["pref"] == "a b;c=d"
    assert client._cookies["pref"] == "a%20b%3Bc%3Dd"


def test_a_quoted_value_is_unquoted_on_read():
    client = _client_for('pref="a b"; Path=/')
    assert client.get("/one").cookies == {"pref": "a b"}


def test_a_deletion_removes_the_cookie():
    app = Veloce(openapi_url=None)

    @app.get("/set")
    async def set_it():
        response = Response(body=b"{}", content_type="application/json")
        response.set_cookie("pref", "a b")
        return response

    @app.get("/del")
    async def delete_it():
        response = Response(body=b"{}", content_type="application/json")
        response.delete_cookie("pref")
        return response

    client = TestClient(app)
    client.get("/set")
    assert client.cookies["pref"] == "a b"
    client.get("/del")
    assert "pref" not in client.cookies


# ── the async client behaves identically ─────────────────────────────


async def test_the_async_client_agrees_too():
    """Sync and async share the jar helpers; a fix to one must reach both."""
    async with AsyncTestClient(_app()) as client:
        response = await client.get("/set", params={"v": "a b;c=d"})
        assert response.cookies["pref"] == "a b;c=d"
        assert (await client.get("/read")).json()["pref"] == "a b;c=d"


# ── a signed session survives the round trip ─────────────────────────


def test_a_session_cookie_still_round_trips():
    """Session values are base64 with `=` padding - the shape most at risk."""

    app = Veloce(openapi_url=None)
    app.add_middleware(SessionMiddleware(secret_key="k" * 32))

    @app.get("/login")
    async def login(request) -> dict:
        request.session["user"] = "ana"
        return {}

    @app.get("/me")
    async def me(request) -> dict:
        return {"user": request.session.get("user")}

    client = TestClient(app)
    client.get("/login")
    assert client.get("/me").json() == {"user": "ana"}
