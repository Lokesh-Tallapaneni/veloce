"""Response.set_cookie(partitioned=True) — CHIPS partitioned cookies."""

from __future__ import annotations

from veloce import Response


def _cookie(resp: Response) -> str:
    return resp.headers["Set-Cookie"]


def test_partitioned_emitted_with_secure():
    resp = Response()
    resp.set_cookie("sid", "abc", secure=True, partitioned=True)
    assert "Partitioned" in _cookie(resp)
    assert "Secure" in _cookie(resp)


def test_partitioned_skipped_without_secure():
    resp = Response()
    # Partitioned requires Secure — dropped when secure is False.
    resp.set_cookie("sid", "abc", partitioned=True)
    assert "Partitioned" not in _cookie(resp)


def test_partitioned_default_false():
    resp = Response()
    resp.set_cookie("sid", "abc", secure=True)
    assert "Partitioned" not in _cookie(resp)


def test_partitioned_with_other_attributes():
    resp = Response()
    resp.set_cookie(
        "sid",
        "abc",
        secure=True,
        httponly=True,
        samesite="None",
        partitioned=True,
    )
    c = _cookie(resp)
    assert "Partitioned" in c
    assert "HttpOnly" in c
    assert "SameSite=None" in c
