"""Tests for veloce.http.cookies — parse_cookie / dump_cookie."""

from __future__ import annotations

import pytest

from veloce.http.cookies import dump_cookie, parse_cookie


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
    from veloce import Response

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
