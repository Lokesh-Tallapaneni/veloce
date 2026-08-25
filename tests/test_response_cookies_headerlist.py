"""Response.cookies + Response.headerlist accessors."""

from __future__ import annotations

import pytest

from veloce import Request, Response
from veloce.app.asgi import _build_asgi_headers

# ── cookies ──────────────────────────────────────────────────────────


def test_no_set_cookie_returns_empty_dict():
    assert Response().cookies == {}


def test_single_set_cookie_parsed():
    resp = Response()
    resp.set_cookie("session", "abc123")
    assert resp.cookies == {"session": "abc123"}


def test_multiple_set_cookies_parsed():
    resp = Response()
    resp.set_cookie("a", "1")
    resp.set_cookie("b", "2")
    assert resp.cookies == {"a": "1", "b": "2"}


def test_duplicate_name_keeps_last():
    """Wire behaviour: client also keeps the most-recent value."""
    resp = Response()
    resp.set_cookie("session", "old")
    resp.set_cookie("session", "new")
    assert resp.cookies == {"session": "new"}


def test_cookies_only_takes_name_value_segment():
    """Path/Secure/HttpOnly etc. shouldn't pollute the value."""
    resp = Response()
    resp.set_cookie("k", "v", path="/", secure=True, httponly=True)
    assert resp.cookies == {"k": "v"}


# ── cookies: percent-decoding round-trip (RFC 6265 Sec. 4.1) ─────────

ENCODED_VALUES = ["plain", "a b", "a;b", "a,b", "a=b", "café", "50%", 'q"uote', "", "a/b?c&d"]


@pytest.mark.parametrize("value", ENCODED_VALUES)
def test_cookies_round_trip_what_set_cookie_encoded(value):
    """`Response.cookies` returns the value that was set, not its wire form."""
    resp = Response()
    resp.set_cookie("k", value)
    assert resp.cookies == {"k": value}


@pytest.mark.parametrize("value", ENCODED_VALUES)
def test_response_cookies_match_request_cookies(value):
    """The response jar and the request jar decode one wire value the same way."""
    resp = Response()
    resp.set_cookie("k", value)
    wire = resp.headers["Set-Cookie"].split(";", 1)[0]
    req = Request(method="GET", path="/x", query_string="", headers={"cookie": wire}, body=b"")
    assert resp.cookies["k"] == req.cookies["k"] == value


def test_cookies_value_containing_equals_is_not_split():
    resp = Response()
    resp.set_cookie("k", "a=b=c")
    assert resp.cookies == {"k": "a=b=c"}


def test_cookies_empty_value():
    resp = Response()
    resp.set_cookie("k", "")
    assert resp.cookies == {"k": ""}


def test_cookies_deleted_cookie_reads_empty():
    resp = Response()
    resp.delete_cookie("k")
    assert resp.cookies == {"k": ""}


def test_cookies_multiple_encoded_values():
    resp = Response()
    resp.set_cookie("a", "x y")
    resp.set_cookie("b", "50%")
    assert resp.cookies == {"a": "x y", "b": "50%"}


def test_cookies_ignores_attribute_segments():
    """`Max-Age`/`Path`/`Expires` are attributes, never cookies of their own."""
    resp = Response()
    resp.set_cookie("k", "v", max_age=60, path="/sub", domain="example.com", httponly=True)
    assert resp.cookies == {"k": "v"}


def test_cookies_reads_lowercase_header_spelling():
    resp = Response()
    resp.headers["set-cookie"] = "k=a%20b; Path=/"
    assert resp.cookies == {"k": "a b"}


# ── headerlist ───────────────────────────────────────────────────────


def test_headerlist_flattens_simple_headers():
    resp = Response(headers={"X-A": "1", "X-B": "2"})
    rows = resp.headerlist
    pairs = {(k, v) for k, v in rows}
    assert ("X-A", "1") in pairs
    assert ("X-B", "2") in pairs


def test_headerlist_expands_multi_set_cookie():
    resp = Response()
    resp.set_cookie("a", "1")
    resp.set_cookie("b", "2")
    set_cookies = [(k, v) for k, v in resp.headerlist if k == "Set-Cookie"]
    assert len(set_cookies) == 2
    values = {v.split(";", 1)[0] for _, v in set_cookies}
    assert values == {"a=1", "b=2"}


def test_headerlist_preserves_other_headers_alongside_set_cookie():
    resp = Response()
    resp.set_cookie("k", "v")
    resp.headers["X-Custom"] = "yes"
    rows = resp.headerlist
    assert ("X-Custom", "yes") in [(k, v) for k, v in rows]


# ── headerlist: the wire emit's fold + CRLF guard ────────────────────


def test_headerlist_folds_duplicate_field_name_spellings():
    """RFC 9110 Sec. 5.1: one field, one entry - the last spelling wins."""
    resp = Response()
    resp.headers["Content-Security-Policy"] = "default-src 'self'"
    resp.headers["content-security-policy"] = "default-src *"
    csp = [(k, v) for k, v in resp.headerlist if k.lower() == "content-security-policy"]
    assert csp == [("content-security-policy", "default-src *")]


def test_headerlist_fold_keeps_the_first_occurrence_position():
    resp = Response(headers={"X-A": "1"})
    resp.headers["ETag"] = '"v1"'
    resp.headers["X-B"] = "2"
    resp.headers["etag"] = '"v2"'
    assert resp.headerlist == [("X-A", "1"), ("etag", '"v2"'), ("X-B", "2")]


def test_headerlist_matches_the_asgi_emit_after_folding():
    resp = Response()
    resp.headers["Content-Security-Policy"] = "default-src 'self'"
    resp.headers["content-security-policy"] = "default-src *"
    resp.set_cookie("a", "1")
    emitted, _ct, _cl = _build_asgi_headers(resp.headers)
    assert [(k.lower(), v) for k, v in resp.headerlist] == [
        (k.decode(), v.decode("latin-1")) for k, v in emitted
    ]


def test_headerlist_does_not_fold_set_cookie():
    """`Set-Cookie` is legitimately multi-valued (RFC 6265 Sec. 3)."""
    resp = Response()
    resp.set_cookie("a", "1")
    resp.set_cookie("b", "2")
    assert len([1 for k, _v in resp.headerlist if k == "Set-Cookie"]) == 2


def test_headerlist_rejects_crlf_in_a_header_value():
    resp = Response()
    resp.headers["X-Bad"] = "a\r\nInjected: 1"
    with pytest.raises(ValueError, match="X-Bad header value"):
        resp.headerlist


def test_headerlist_rejects_crlf_in_a_header_name():
    resp = Response()
    resp.headers["X-Bad\r\nInjected"] = "1"
    with pytest.raises(ValueError, match="header name"):
        resp.headerlist


def test_headerlist_rejects_nul_in_a_header_value():
    resp = Response()
    resp.headers["X-Bad"] = "a\x00b"
    with pytest.raises(ValueError, match="X-Bad header value"):
        resp.headerlist


def test_headerlist_rejects_crlf_injected_into_a_set_cookie_header():
    resp = Response()
    resp.headers["Set-Cookie"] = "k=v\r\nX-Injected: 1"
    with pytest.raises(ValueError, match="Set-Cookie value"):
        resp.headerlist


def test_headers_stays_the_raw_unfolded_view():
    resp = Response()
    resp.headers["X-A"] = "1"
    resp.headers["x-a"] = "2"
    assert dict(resp.headers) == {"X-A": "1", "x-a": "2"}


# ── One reader for a header, whatever casing wrote it ────────────────


class _Conditional:
    """The precondition fields `make_conditional` / `check_preconditions` read."""

    def __init__(self, if_none_match: tuple = (), if_match: tuple = ()) -> None:
        self.if_none_match = if_none_match
        self.if_match = if_match
        self.if_modified_since = None


def _tagged(spelling: str) -> Response:
    """A response whose ETag was written under `spelling`."""
    return Response(200, b"body", headers={spelling: '"abc"'})


SPELLINGS = ["ETag", "etag", "ETAG", "Etag", "eTaG"]


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_get_etag_reads_every_casing(spelling: str):
    """HTTP field names are case-insensitive (RFC 9110 section 5.1)."""
    assert _tagged(spelling).get_etag() == ('"abc"', False)


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_an_if_match_the_response_satisfies_is_not_refused(spelling: str):
    """A spurious 412 is the lost-update guard rejecting a legitimate write."""
    response = _tagged(spelling)
    assert response.check_preconditions(_Conditional(if_match=('"abc"',))) is response


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_an_if_match_the_response_fails_is_still_refused(spelling: str):
    from veloce.exceptions import PreconditionFailed

    with pytest.raises(PreconditionFailed):
        _tagged(spelling).check_preconditions(_Conditional(if_match=('"other"',)))


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_conditional_get_matches_under_every_casing(spelling: str):
    response = _tagged(spelling)
    response.make_conditional(_Conditional(if_none_match=('"abc"',)))
    assert response.status_code == 304


@pytest.mark.parametrize("spelling", ["Last-Modified", "last-modified", "LAST-MODIFIED"])
def test_last_modified_reads_every_casing(spelling: str):
    response = Response(200, b"body", headers={spelling: "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert response.last_modified is not None


@pytest.mark.parametrize("spelling", ["Expires", "expires", "EXPIRES"])
def test_expires_reads_every_casing(spelling: str):
    response = Response(200, b"body", headers={spelling: "Wed, 21 Oct 2015 07:28:00 GMT"})
    assert response.expires is not None


@pytest.mark.parametrize("spelling", ["Vary", "vary", "VARY"])
def test_add_vary_merges_with_whatever_wrote_the_header(spelling: str):
    response = Response(200, b"body", headers={spelling: "Accept"})
    merged = response.add_vary("Accept-Encoding")
    assert "Accept" in merged
    assert "Accept-Encoding" in merged


@pytest.mark.parametrize("spelling", ["Vary", "vary", "VARY"])
def test_add_vary_does_not_duplicate_across_casings(spelling: str):
    response = Response(200, b"body", headers={spelling: "Accept"})
    assert response.add_vary("Accept").count("Accept") == 1
