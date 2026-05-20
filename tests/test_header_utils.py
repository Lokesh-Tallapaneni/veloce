"""Header value parsers + Request.access_route."""

from __future__ import annotations

from veloce import Request
from veloce.http.header_utils import (
    dump_options_header,
    parse_etags,
    parse_options_header,
    parse_set_header,
)

# ── parse_options_header ────────────────────────────────────────────


def test_parse_options_header_content_type():
    assert parse_options_header("text/html; charset=utf-8") == (
        "text/html",
        {"charset": "utf-8"},
    )


def test_parse_options_header_quoted_value():
    assert parse_options_header('attachment; filename="report.pdf"') == (
        "attachment",
        {"filename": "report.pdf"},
    )


def test_parse_options_header_empty_value():
    assert parse_options_header("") == ("", {})


def test_parse_options_header_multiple_params():
    primary, opts = parse_options_header('form-data; name="file"; filename="x.txt"')
    assert primary == "form-data"
    assert opts == {"name": "file", "filename": "x.txt"}


def test_parse_options_header_bare_token_option():
    # `gzip; q=0` is the common shape, but bare tokens (no `=`) appear.
    primary, opts = parse_options_header("text/html; charset")
    assert primary == "text/html"
    assert opts == {"charset": ""}


def test_parse_options_header_lower_cases_keys():
    _, opts = parse_options_header("text/html; Charset=UTF-8")
    assert "charset" in opts
    # Value case preserved.
    assert opts["charset"] == "UTF-8"


# ── dump_options_header ─────────────────────────────────────────────


def test_dump_options_header_roundtrip():
    primary = "attachment"
    opts = {"filename": "report.pdf"}
    out = dump_options_header(primary, opts)
    assert parse_options_header(out) == (primary, opts)


def test_dump_options_header_quotes_when_needed():
    out = dump_options_header("form-data", {"filename": "my file.txt"})
    assert 'filename="my file.txt"' in out


def test_dump_options_header_no_quotes_for_safe_value():
    out = dump_options_header("text/html", {"charset": "utf-8"})
    assert "charset=utf-8" in out


def test_dump_options_header_empty_value_emits_bare_token():
    out = dump_options_header("foo", {"flag": ""})
    assert out == "foo; flag"


# ── parse_set_header ────────────────────────────────────────────────


def test_parse_set_header_returns_frozenset():
    out = parse_set_header("gzip, deflate, br")
    assert isinstance(out, frozenset)
    assert out == frozenset({"gzip", "deflate", "br"})


def test_parse_set_header_lowercases_tokens():
    assert parse_set_header("Gzip, Deflate") == frozenset({"gzip", "deflate"})


def test_parse_set_header_empty():
    assert parse_set_header("") == frozenset()


def test_parse_set_header_skips_blank_entries():
    assert parse_set_header("gzip, , deflate,") == frozenset({"gzip", "deflate"})


# ── parse_etags ─────────────────────────────────────────────────────


def test_parse_etags_strong_single():
    assert parse_etags('"abc123"') == [("abc123", False)]


def test_parse_etags_weak_single():
    assert parse_etags('W/"abc123"') == [("abc123", True)]


def test_parse_etags_multiple_mixed():
    out = parse_etags('"a", W/"b", "c"')
    assert out == [("a", False), ("b", True), ("c", False)]


def test_parse_etags_star():
    assert parse_etags("*") == [("*", False)]


def test_parse_etags_empty():
    assert parse_etags("") == []


# ── Request.access_route ────────────────────────────────────────────


def test_access_route_uses_x_forwarded_for():
    """Chain order: client → proxies, then peer last."""
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={"X-Forwarded-For": "1.1.1.1, 2.2.2.2"},
        body=b"",
        scope={"client": ("3.3.3.3", 0)},
    )
    assert req.access_route == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]


def test_access_route_falls_back_to_peer():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={},
        body=b"",
        scope={"client": ("3.3.3.3", 0)},
    )
    assert req.access_route == ["3.3.3.3"]


def test_access_route_empty_when_no_peer():
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.access_route == []
