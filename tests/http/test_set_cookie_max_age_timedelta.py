"""Response.set_cookie(max_age=timedelta) — timedelta coercion."""

from __future__ import annotations

from datetime import timedelta

from veloce import Response


def _cookie(resp: Response) -> str:
    return resp.headers["Set-Cookie"]


def test_max_age_int_unchanged():
    resp = Response()
    resp.set_cookie("s", "v", max_age=3600)
    assert "Max-Age=3600" in _cookie(resp)


def test_max_age_timedelta_coerced_to_seconds():
    resp = Response()
    resp.set_cookie("s", "v", max_age=timedelta(hours=2))
    assert "Max-Age=7200" in _cookie(resp)


def test_max_age_timedelta_days():
    resp = Response()
    resp.set_cookie("s", "v", max_age=timedelta(days=1))
    assert "Max-Age=86400" in _cookie(resp)


def test_max_age_timedelta_fractional_truncates():
    resp = Response()
    resp.set_cookie("s", "v", max_age=timedelta(seconds=90, milliseconds=500))
    # Whole seconds only on the wire.
    assert "Max-Age=90" in _cookie(resp)


def test_max_age_none_omits_attribute():
    resp = Response()
    resp.set_cookie("s", "v")
    assert "Max-Age" not in _cookie(resp)
