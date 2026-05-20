"""parse_cookie / dump_cookie — RFC 6265 cookie string helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from veloce.http.cookies import dump_cookie, parse_cookie

# ── parse_cookie ────────────────────────────────────────────────────


def test_parse_cookie_single_pair():
    assert parse_cookie("session=abc123") == {"session": "abc123"}


def test_parse_cookie_multiple_pairs():
    assert parse_cookie("a=1; b=2; c=3") == {"a": "1", "b": "2", "c": "3"}


def test_parse_cookie_empty():
    assert parse_cookie("") == {}
    assert parse_cookie(None) == {}


def test_parse_cookie_skips_attribute_segments():
    # No `=` → skipped.
    assert parse_cookie("a=1; HttpOnly; b=2") == {"a": "1", "b": "2"}


def test_parse_cookie_percent_decodes_value():
    assert parse_cookie("name=hello%20world") == {"name": "hello world"}


def test_parse_cookie_first_occurrence_wins():
    assert parse_cookie("x=first; x=second") == {"x": "first"}


def test_parse_cookie_strips_quotes():
    assert parse_cookie('token="quoted"') == {"token": "quoted"}


# ── dump_cookie ─────────────────────────────────────────────────────


def test_dump_cookie_basic():
    assert dump_cookie("session", "abc") == "session=abc; Path=/"


def test_dump_cookie_quotes_value_with_spaces():
    out = dump_cookie("msg", "hello world")
    assert "hello%20world" in out


def test_dump_cookie_max_age_int():
    out = dump_cookie("s", "v", max_age=3600)
    assert "Max-Age=3600" in out


def test_dump_cookie_max_age_timedelta():
    out = dump_cookie("s", "v", max_age=timedelta(hours=2))
    assert "Max-Age=7200" in out


def test_dump_cookie_expires_datetime():
    dt = datetime(1994, 11, 6, 8, 49, 37, tzinfo=timezone.utc)
    out = dump_cookie("s", "v", expires=dt)
    assert "Expires=Sun, 06 Nov 1994 08:49:37 GMT" in out


def test_dump_cookie_secure_and_httponly():
    out = dump_cookie("s", "v", secure=True, httponly=True)
    assert "Secure" in out
    assert "HttpOnly" in out


def test_dump_cookie_domain_and_path():
    out = dump_cookie("s", "v", path="/admin", domain="example.com")
    assert "Path=/admin" in out
    assert "Domain=example.com" in out


def test_dump_cookie_samesite_normalised():
    assert "SameSite=Lax" in dump_cookie("s", "v", samesite="lax")
    assert "SameSite=Strict" in dump_cookie("s", "v", samesite="STRICT")


def test_dump_cookie_samesite_invalid_raises():
    with pytest.raises(ValueError, match="samesite"):
        dump_cookie("s", "v", samesite="bogus")


def test_dump_cookie_no_path_when_none():
    out = dump_cookie("s", "v", path=None)
    assert "Path" not in out


def test_dump_cookie_roundtrips_with_parse_cookie():
    out = dump_cookie("name", "hello world")
    # Strip attributes, parse the name=value bit back.
    pair = out.split(";", 1)[0]
    assert parse_cookie(pair) == {"name": "hello world"}
