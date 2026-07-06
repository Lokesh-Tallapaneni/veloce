"""Response.set_cookie expires + delete_cookie attributes."""

from __future__ import annotations

import datetime as dt

from veloce.http.response import Response


def _cookie(resp: Response) -> str:
    return resp.headers["Set-Cookie"]


# ── expires forms ────────────────────────────────────────────────────


def test_expires_accepts_datetime_utc():
    resp = Response()
    resp.set_cookie("k", "v", expires=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc))
    assert "Expires=Tue, 01 Jan 2030 00:00:00 GMT" in _cookie(resp)


def test_expires_accepts_naive_datetime_as_utc():
    """A naive datetime is interpreted as UTC (the safe default)."""
    resp = Response()
    resp.set_cookie("k", "v", expires=dt.datetime(2030, 1, 1))
    assert "Expires=Tue, 01 Jan 2030 00:00:00 GMT" in _cookie(resp)


def test_expires_accepts_unix_timestamp():
    resp = Response()
    resp.set_cookie("k", "v", expires=1893456000)  # 2030-01-01 UTC
    assert "Expires=Tue, 01 Jan 2030 00:00:00 GMT" in _cookie(resp)


def test_expires_accepts_preformatted_string():
    resp = Response()
    resp.set_cookie("k", "v", expires="Tue, 01 Jan 2030 00:00:00 GMT")
    assert "Expires=Tue, 01 Jan 2030 00:00:00 GMT" in _cookie(resp)


def test_max_age_and_expires_both_emitted():
    """RFC 6265 §5.2.2: both directives may co-exist; clients prefer Max-Age."""
    resp = Response()
    resp.set_cookie("k", "v", max_age=600, expires=1893456000)
    c = _cookie(resp)
    assert "Max-Age=600" in c
    assert "Expires=" in c


# ── delete_cookie passes through the flag set ────────────────────────


def test_delete_cookie_emits_secure_samesite_httponly():
    """Browser only treats it as a delete if the attribute set matches."""
    resp = Response()
    resp.delete_cookie("session", secure=True, httponly=True, samesite="None")
    c = _cookie(resp)
    assert "session=" in c
    assert "Max-Age=0" in c
    assert "Secure" in c
    assert "HttpOnly" in c
    assert "SameSite=None" in c


def test_delete_cookie_minimal_form_unchanged():
    """Back-compat: default `delete_cookie(key)` still works."""
    resp = Response()
    resp.delete_cookie("session")
    c = _cookie(resp)
    assert "Max-Age=0" in c
    assert "Secure" not in c
    assert "HttpOnly" not in c


# ── samesite default (S8) ────────────────────────────────────────────


def test_set_cookie_defaults_to_samesite_lax():
    """A CSRF-resistant default that matches modern browser behaviour."""
    resp = Response()
    resp.set_cookie("k", "v")
    assert "SameSite=Lax" in _cookie(resp)


def test_set_cookie_samesite_override():
    resp = Response()
    resp.set_cookie("k", "v", samesite="Strict")
    assert "SameSite=Strict" in _cookie(resp)


class TestDeleteCookie:
    def test_delete_cookie(self):
        resp = Response(status_code=200, body=b"ok")
        resp.delete_cookie("session")
        assert "Max-Age=0" in resp.headers["Set-Cookie"]

    def test_multiple_cookies(self):
        resp = Response(status_code=200, body=b"ok")
        resp.set_cookie("a", "1")
        resp.set_cookie("b", "2")
        cookie_header = resp.headers["Set-Cookie"]
        assert "a=1" in cookie_header
        assert "b=2" in cookie_header


def test_set_cookie_samesite_none_omits_attribute():
    """Explicit `samesite=None` drops the attribute entirely."""
    resp = Response()
    resp.set_cookie("k", "v", samesite=None)
    assert "SameSite" not in _cookie(resp)
