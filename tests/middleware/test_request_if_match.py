"""Request.if_match and Request.if_unmodified_since."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Request


def _req(headers: dict) -> Request:
    return make_request(method="PUT", path="/x", query_string="", headers=headers, body=b"")


# ── if_match ─────────────────────────────────────────────────────────


def test_if_match_missing_returns_empty_tuple():
    assert _req({}).if_match == ()


def test_if_match_wildcard():
    assert _req({"if-match": "*"}).if_match == ("*",)


def test_if_match_single_etag():
    assert _req({"if-match": '"v1"'}).if_match == ('"v1"',)


def test_if_match_multiple_etags():
    r = _req({"if-match": '"v1", "v2", W/"weak"'})
    assert r.if_match == ('"v1"', '"v2"', 'W/"weak"')


def test_if_match_comma_inside_quoted_tag_not_split():
    """RFC 9110 §8.8.3 etagc permits a comma inside the opaque tag."""
    r = _req({"if-match": '"abc,def"'})
    assert r.if_match == ('"abc,def"',)


def test_if_match_quoted_comma_with_list():
    r = _req({"if-match": '"a,b", "c"'})
    assert r.if_match == ('"a,b"', '"c"')


# ── if_unmodified_since ──────────────────────────────────────────────


def test_if_unmodified_since_missing_returns_none():
    assert _req({}).if_unmodified_since is None


def test_if_unmodified_since_parses_imf_fixdate():
    """RFC 9110 §5.6.7 IMF-fixdate format."""
    r = _req({"if-unmodified-since": "Tue, 01 Jan 2030 00:00:00 GMT"})
    # 2030-01-01 00:00 UTC == 1893456000
    assert r.if_unmodified_since == 1893456000.0


def test_if_unmodified_since_malformed_returns_none():
    assert _req({"if-unmodified-since": "not-a-date"}).if_unmodified_since is None
