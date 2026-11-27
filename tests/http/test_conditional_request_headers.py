"""Parsed If-Modified-Since + If-None-Match (Q27)."""

from __future__ import annotations

from email.utils import formatdate

from tests.conftest import make_request
from veloce import Request


def _req(headers: dict[str, str]) -> Request:
    return make_request(method="GET", path="/", query_string="", headers=headers, body=b"")


# ── if_modified_since ─────────────────────────────────────────────────


def test_if_modified_since_parses_imf_fixdate():
    ts = 1_700_000_000
    raw = formatdate(ts, usegmt=True)
    assert _req({"if-modified-since": raw}).if_modified_since == ts


def test_if_modified_since_missing_returns_none():
    assert _req({}).if_modified_since is None


def test_if_modified_since_invalid_returns_none():
    """Garbage date → None, no raise."""
    assert _req({"if-modified-since": "not a date"}).if_modified_since is None


def test_if_modified_since_empty_string_returns_none():
    assert _req({"if-modified-since": ""}).if_modified_since is None


def test_if_modified_since_whitespace_tolerated():
    ts = 1_700_000_000
    raw = "  " + formatdate(ts, usegmt=True) + "  "
    assert _req({"if-modified-since": raw}).if_modified_since == ts


# ── if_none_match ─────────────────────────────────────────────────────


def test_if_none_match_single_etag():
    req = _req({"if-none-match": '"abc123"'})
    assert req.if_none_match == ('"abc123"',)


def test_if_none_match_star_matches_any():
    """`If-None-Match: *` represents the wildcard 'any representation'."""
    assert _req({"if-none-match": "*"}).if_none_match == ("*",)


def test_if_none_match_comma_list():
    req = _req({"if-none-match": '"abc", "def", "xyz"'})
    assert req.if_none_match == ('"abc"', '"def"', '"xyz"')


def test_if_none_match_weak_etag_preserved():
    """Weak ETags (`W/"abc"`) preserve their `W/` prefix so the caller
    can decide how strict they want to be."""
    req = _req({"if-none-match": 'W/"abc"'})
    assert req.if_none_match == ('W/"abc"',)


def test_if_none_match_missing_returns_empty_tuple():
    """Empty tuple, not None — lets callers iterate without a guard."""
    assert _req({}).if_none_match == ()


def test_if_none_match_empty_header_returns_empty_tuple():
    assert _req({"if-none-match": ""}).if_none_match == ()


def test_if_none_match_strips_whitespace_around_entries():
    req = _req({"if-none-match": '  "a"  ,  "b"  '})
    assert req.if_none_match == ('"a"', '"b"')


def test_if_none_match_comma_inside_quoted_tag_not_split():
    """RFC 9110 §8.8.3 etagc permits a comma inside the opaque tag, so a
    quoted tag containing a comma must stay one entry."""
    req = _req({"if-none-match": '"abc,def"'})
    assert req.if_none_match == ('"abc,def"',)


def test_if_none_match_quoted_comma_with_list():
    req = _req({"if-none-match": '"a,b", "c", W/"d,e"'})
    assert req.if_none_match == ('"a,b"', '"c"', 'W/"d,e"')
