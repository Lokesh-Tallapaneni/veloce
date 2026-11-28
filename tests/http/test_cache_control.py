"""CacheControl header parser."""

from __future__ import annotations

from veloce import Request, Response
from veloce.http.cache_control import CacheControl

# ── CacheControl parser ──────────────────────────────────────────────


def test_empty_header_is_falsy():
    cc = CacheControl("")
    assert not cc
    assert cc.max_age is None
    assert cc.no_cache is False


def test_bool_directive_no_cache():
    cc = CacheControl("no-cache")
    assert cc.no_cache is True
    assert cc.no_store is False


def test_max_age_parsed_as_int():
    cc = CacheControl("max-age=3600")
    assert cc.max_age == 3600


def test_non_numeric_int_directive_is_dropped():
    # `max-age=abc` is malformed; the attribute stays int-or-None rather
    # than returning a raw str that breaks `cc.max_age > 0`.
    cc = CacheControl("max-age=abc")
    assert cc.max_age is None
    assert "max-age" not in cc.to_header()


def test_combined_directives():
    cc = CacheControl("public, max-age=600, must-revalidate")
    assert cc.public is True
    assert cc.max_age == 600
    assert cc.must_revalidate is True
    assert cc.private is False


def test_quoted_value_unquoted():
    cc = CacheControl('private, max-age="120"')
    assert cc.private is True
    assert cc.max_age == 120


def test_s_maxage_alias():
    cc = CacheControl("s-maxage=300")
    assert cc.s_maxage == 300


def test_contains_uses_wire_or_attr_name():
    cc = CacheControl("max-age=10, no-cache")
    assert "max-age" in cc
    assert "max_age" in cc
    assert "no-cache" in cc
    assert "no_cache" in cc
    assert "private" not in cc


def test_setattr_adds_directive():
    cc = CacheControl("")
    cc.max_age = 60
    cc.no_store = True
    header = cc.to_header()
    assert "max-age=60" in header
    assert "no-store" in header


def test_setattr_false_removes_directive():
    cc = CacheControl("no-cache, max-age=10")
    cc.no_cache = False
    assert cc.no_cache is False
    assert "no-cache" not in cc.to_header()


def test_setattr_none_removes_directive():
    cc = CacheControl("max-age=10")
    cc.max_age = None
    assert cc.max_age is None
    assert "max-age" not in cc.to_header()


def test_str_roundtrips_to_header():
    cc = CacheControl("public, max-age=120, no-transform")
    assert str(cc) == "public, max-age=120, no-transform"


def test_repr_contains_header_string():
    cc = CacheControl("no-cache")
    assert "no-cache" in repr(cc)


# ── Request.cache_control ───────────────────────────────────────────


def test_request_cache_control_reads_header():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={"Cache-Control": "no-store, max-age=0"},
        body=b"",
    )
    cc = req.cache_control
    assert cc.no_store is True
    assert cc.max_age == 0


def test_request_cache_control_empty_when_no_header():
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.cache_control.max_age is None
    assert req.cache_control.no_cache is False


# ── Response.cache_control ──────────────────────────────────────────


def test_response_cache_control_reads_header():
    resp = Response()
    resp.headers["Cache-Control"] = "public, max-age=3600"
    cc = resp.cache_control
    assert cc.public is True
    assert cc.max_age == 3600


def test_response_set_cache_control_then_read():
    """`set_cache_control` writes; `.cache_control` reads back."""
    resp = Response()
    resp.set_cache_control(max_age=300, public=True)
    cc = resp.cache_control
    assert cc.public is True
    assert cc.max_age == 300
