"""Response.add_etag + Response.make_conditional — conditional-response helpers."""

from __future__ import annotations

from veloce import Request, Response


def _req(headers: dict | None = None) -> Request:
    return Request(method="GET", path="/", query_string="", headers=headers or {}, body=b"")


# ── add_etag ─────────────────────────────────────────────────────────


def test_add_etag_sets_strong_quoted_etag():
    resp = Response(body=b"hello world")
    tag = resp.add_etag()
    # MD5 of "hello world".
    assert tag == '"5eb63bbbe01eeed093cb22bb8f5acdc3"'
    assert resp.headers["ETag"] == tag


def test_add_etag_weak_prefix():
    resp = Response(body=b"hello")
    tag = resp.add_etag(weak=True)
    assert tag.startswith('W/"')


def test_add_etag_different_bodies_different_tags():
    a = Response(body=b"a")
    b = Response(body=b"b")
    a.add_etag()
    b.add_etag()
    assert a.headers["ETag"] != b.headers["ETag"]


# ── make_conditional: If-None-Match ──────────────────────────────────


def test_conditional_304_when_if_none_match_matches():
    resp = Response(body=b"hello")
    resp.add_etag()
    req = _req({"if-none-match": resp.headers["ETag"]})
    result = resp.make_conditional(req)
    assert result is resp
    assert result.status_code == 304
    assert result.body == b""


def test_conditional_304_when_if_none_match_wildcard():
    resp = Response(body=b"hi")
    resp.add_etag()
    req = _req({"if-none-match": "*"})
    resp.make_conditional(req)
    assert resp.status_code == 304


def test_conditional_no_change_when_if_none_match_does_not_match():
    resp = Response(body=b"hi")
    resp.add_etag()
    req = _req({"if-none-match": '"different-tag"'})
    resp.make_conditional(req)
    assert resp.status_code == 200
    assert resp.body == b"hi"


def test_conditional_strong_compare_strips_weak_prefix():
    """W/"x" matches "x" under the strong comparison veloce uses for
    304 downgrade (the default policy)."""
    resp = Response(body=b"hi")
    resp.add_etag()
    weak_inm = "W/" + resp.headers["ETag"]
    req = _req({"if-none-match": weak_inm})
    resp.make_conditional(req)
    assert resp.status_code == 304


# ── make_conditional: If-Modified-Since ──────────────────────────────


def test_conditional_304_when_last_modified_not_newer_than_ims():
    resp = Response(body=b"hi")
    resp.headers["Last-Modified"] = "Mon, 01 Jan 2024 00:00:00 GMT"
    req = _req({"if-modified-since": "Mon, 01 Jan 2024 00:00:00 GMT"})
    resp.make_conditional(req)
    assert resp.status_code == 304


def test_conditional_no_change_when_resource_modified_after_ims():
    resp = Response(body=b"hi")
    resp.headers["Last-Modified"] = "Mon, 01 Feb 2024 00:00:00 GMT"
    req = _req({"if-modified-since": "Mon, 01 Jan 2024 00:00:00 GMT"})
    resp.make_conditional(req)
    assert resp.status_code == 200


def test_if_none_match_supersedes_if_modified_since():
    """RFC 9110 §13.2 precedence — INM wins when both are present."""
    resp = Response(body=b"hi")
    resp.add_etag()
    resp.headers["Last-Modified"] = "Mon, 01 Jan 2000 00:00:00 GMT"
    req = _req(
        {
            "if-none-match": '"wrong-tag"',
            "if-modified-since": "Mon, 01 Jan 2030 00:00:00 GMT",
        }
    )
    resp.make_conditional(req)
    # INM didn't match → no 304, even though IMS would have triggered one.
    assert resp.status_code == 200
