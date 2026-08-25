"""Replacing a response header finds it under whatever casing it was stored.

`Response.headers` is a plain dict, chosen for speed, so casing matters to a
lookup. Every site that *replaces* a header hand-rolled its own case handling,
and each covered only the spellings its author thought of — the canonical one
and, sometimes, the lower-case one.

CORS was the one that lost data. `Access-Control-Expose-Headers` is a list header
other middleware legitimately contribute to, and the merge checked exactly two
casings:

    contributed as Access-Control-Expose-Headers  -> X-Request-Id, X-Total   ok
    contributed as access-control-expose-headers  -> X-Request-Id, X-Total   ok
    contributed as ACCESS-CONTROL-EXPOSE-HEADERS  -> X-Total                 lost
    contributed as Access-control-expose-headers  -> X-Total                 lost

The contribution was silently discarded — which is the exact failure the comment
above that code says the merge exists to prevent. `Allow` and `Content-Length`
had the same single-casing `pop`.

`header_pop` is the replacement half of the existing `header_get` /
`header_present` / `header_key` family, and those sites use it.

**`add_vary` was left out at first, on reasoning that was wrong.** Its fast path
probed only `Vary` and `vary`, on the argument that a third casing merely produced
two `Vary` field lines, which RFC 9110 Sec. 5.2 says a recipient combines. It does
not reach the recipient. Both emit paths fold duplicate field names and keep the
last write, so one line is sent and the earlier value is dropped:

    headers   {'VARY': 'Cookie', 'Vary': 'Accept-Encoding'}
    wire      Vary: Accept-Encoding

`Vary: Cookie` gone is not a tidiness problem - it is how a shared cache serves
one user's response to another. `add_vary` now does the same exact scan as the
rest of the family, and reuses its result for the merge path. Measured cost:
+0.26 us per call, low single digits on a bare CORS request, within noise on a
session request.
"""

from __future__ import annotations

import pytest

from veloce import CORSMiddleware, Response, Veloce
from veloce.http.response import header_get, header_key, header_pop, header_present
from veloce.testclient import TestClient

#: Written as a constant so the escape survives every editing round trip.
CRLF = bytes((13, 10))

#: Spellings a contributor might plausibly use.
CASINGS = [
    "Access-Control-Expose-Headers",
    "access-control-expose-headers",
    "ACCESS-CONTROL-EXPOSE-HEADERS",
    "Access-control-expose-headers",
    "aCCESS-cONTROL-eXPOSE-hEADERS",
]


def _cors_app(contributed_as: str | None):
    app = Veloce(openapi_url=None)
    app.add_middleware(
        CORSMiddleware(allow_origins=["https://a.example"], expose_headers=["X-Total"])
    )

    @app.get("/x")
    async def x():
        headers = {contributed_as: "X-Request-Id"} if contributed_as else {}
        return Response(body=b"{}", content_type="application/json", headers=headers)

    return app


def _exposed(response) -> set[str]:
    for key, value in response.headers.items():
        if key.lower() == "access-control-expose-headers":
            return {part.strip() for part in value.split(",")}
    return set()


# ── CORS keeps every contribution ────────────────────────────────────


@pytest.mark.parametrize("casing", CASINGS)
def test_a_contribution_survives_under_any_casing(casing):
    """The defect: three of these five had the contribution discarded."""
    response = TestClient(_cors_app(casing)).get("/x", headers={"Origin": "https://a.example"})
    assert _exposed(response) == {"X-Request-Id", "X-Total"}


@pytest.mark.parametrize("casing", CASINGS)
def test_only_one_expose_header_is_emitted(casing):
    """Merging must replace, not append a second field line."""
    response = TestClient(_cors_app(casing)).get("/x", headers={"Origin": "https://a.example"})
    names = [k for k in response.headers if k.lower() == "access-control-expose-headers"]
    assert len(names) == 1


def test_no_contribution_still_exposes_the_configured_headers():
    response = TestClient(_cors_app(None)).get("/x", headers={"Origin": "https://a.example"})
    assert _exposed(response) == {"X-Total"}


def test_a_duplicate_entry_is_not_repeated():
    """`HeaderSet` dedups; the merge must not reintroduce a duplicate."""
    app = _cors_app("Access-Control-Expose-Headers")

    @app.get("/dup")
    async def dup():
        return Response(
            body=b"{}",
            content_type="application/json",
            headers={"access-control-expose-headers": "X-Total"},
        )

    response = TestClient(app).get("/dup", headers={"Origin": "https://a.example"})
    value = next(v for k, v in response.headers.items() if k.lower().startswith("access-control-e"))
    assert value.count("X-Total") == 1


def test_a_non_cors_request_is_untouched():
    """No `Origin`, no CORS headers - the negative case for the whole middleware."""
    response = TestClient(_cors_app("Access-Control-Expose-Headers")).get("/x")
    assert _exposed(response) == {"X-Request-Id"}


# ── the helper itself ────────────────────────────────────────────────


@pytest.mark.parametrize("stored", ["Vary", "vary", "VARY", "vAry"])
def test_header_pop_finds_any_casing(stored):
    headers = {stored: "Accept-Encoding", "Content-Type": "text/html"}
    assert header_pop(headers, "Vary") == "Accept-Encoding"
    assert headers == {"Content-Type": "text/html"}


def test_header_pop_returns_none_when_absent():
    headers = {"Content-Type": "text/html"}
    assert header_pop(headers, "Vary") is None
    assert headers == {"Content-Type": "text/html"}


def test_header_pop_on_an_empty_mapping():
    headers: dict[str, str] = {}
    assert header_pop(headers, "Vary") is None


def test_header_pop_removes_only_the_named_header():
    headers = {"VARY": "a", "Allow": "GET", "X-Other": "z"}
    header_pop(headers, "Vary")
    assert headers == {"Allow": "GET", "X-Other": "z"}


def test_the_family_agrees():
    """`header_key` / `header_get` / `header_present` / `header_pop`, one rule."""
    headers = {"VARY": "a"}
    assert header_key(headers, "Vary") == "VARY"
    assert header_get(headers, "Vary") == "a"
    assert header_present(headers, "Vary") is True
    assert header_pop(headers, "Vary") == "a"
    assert header_present(headers, "Vary") is False


# ── Allow and Content-Length replace rather than duplicate ───────────


@pytest.mark.parametrize("stored", ["Allow", "allow", "ALLOW"])
def test_setting_allow_replaces_any_casing(stored):
    response = Response(body=b"{}", headers={stored: "GET"})
    response.allow = ["GET", "POST"]
    names = [k for k in response.headers if k.lower() == "allow"]
    assert len(names) == 1
    assert set(response.headers[names[0]].split(", ")) == {"GET", "POST"}


@pytest.mark.parametrize("stored", ["Content-Length", "content-length", "CONTENT-LENGTH"])
def test_a_304_downgrade_leaves_one_content_length(stored):
    """Two `Content-Length` field lines is a framing hazard, not a tidiness issue."""
    response = Response(body=b"hello", headers={stored: "5", "ETag": '"abc"'})
    response._downgrade_to_304()
    names = [k for k in response.headers if k.lower() == "content-length"]
    assert len(names) == 1
    assert response.headers[names[0]] == "5"
    assert response.status_code == 304
    assert response.body == b""


def test_a_conditional_request_still_downgrades():
    """End to end: the same path through the public entry point."""
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(request):
        response = Response(body=b"hello", content_type="text/plain")
        response.set_etag("abc")
        return response.make_conditional(request)

    client = TestClient(app)
    etag = client.get("/x").headers["ETag"]
    conditional = client.get("/x", headers={"If-None-Match": etag})
    assert conditional.status_code == 304
    assert len([k for k in conditional.headers if k.lower() == "content-length"]) == 1


# ── add_vary merges under every casing ───────────────────────

VARY_CASINGS = ["Vary", "vary", "VARY", "vAry", "vARY"]


@pytest.mark.parametrize("stored", VARY_CASINGS)
def test_add_vary_merges_any_casing(stored):
    """The defect: the last three took the fast path and orphaned the value."""
    response = Response(body=b"{}", headers={stored: "Accept-Encoding"})
    response.add_vary("Origin")
    names = [k for k in response.headers if k.lower() == "vary"]
    assert len(names) == 1
    assert set(response.headers[names[0]].split(", ")) == {"Accept-Encoding", "Origin"}


@pytest.mark.parametrize("stored", VARY_CASINGS)
def test_the_merged_vary_reaches_the_native_wire(stored):
    """Where the loss actually showed: one field line, carrying both values."""
    response = Response(body=b"x", content_type="text/plain", headers={stored: "Cookie"})
    response.add_vary("Accept-Encoding")
    lines = [line for line in response.encode().split(CRLF) if line.lower().startswith(b"vary")]
    assert len(lines) == 1
    assert set(lines[0].split(b": ", 1)[1].split(b", ")) == {b"Cookie", b"Accept-Encoding"}


@pytest.mark.parametrize("stored", VARY_CASINGS)
def test_the_merged_vary_reaches_the_headerlist(stored):
    """`headerlist` is what the ASGI emit path folds; it must carry both."""
    response = Response(body=b"{}", headers={stored: "Cookie"})
    response.add_vary("Accept-Encoding")
    values = [v for k, v in response.headerlist if k.lower() == "vary"]
    assert len(values) == 1
    assert set(values[0].split(", ")) == {"Cookie", "Accept-Encoding"}


def test_a_session_response_keeps_a_hand_written_vary():
    """End to end: the middleware that runs `add_vary` on every response."""
    from veloce import SessionMiddleware

    app = Veloce(openapi_url=None)
    app.config["SECRET_KEY"] = "k"
    app.add_middleware(SessionMiddleware(secret_key="k" * 32))

    @app.get("/x")
    async def x(request):
        request.session["n"] = 1
        return Response(body=b"{}", content_type="application/json", headers={"VARY": "Origin"})

    response = TestClient(app).get("/x")
    values = [v for k, v in response.headers.items() if k.lower() == "vary"]
    assert len(values) == 1
    assert "Origin" in values[0]


def test_add_vary_merges_when_several_names_are_added():
    """The multi-name path takes the merge branch, which is case-insensitive."""
    response = Response(body=b"{}", headers={"VARY": "Accept-Encoding"})
    response.add_vary("Origin", "Accept")
    names = [k for k in response.headers if k.lower() == "vary"]
    assert len(names) == 1
    assert set(response.headers[names[0]].split(", ")) == {
        "Accept-Encoding",
        "Origin",
        "Accept",
    }


def test_add_vary_on_a_response_with_none():
    response = Response(body=b"{}")
    response.add_vary("Origin")
    assert response.headers["Vary"] == "Origin"


def test_add_vary_does_not_duplicate_an_entry():
    response = Response(body=b"{}", headers={"Vary": "Origin"})
    response.add_vary("Origin")
    assert response.headers["Vary"] == "Origin"
