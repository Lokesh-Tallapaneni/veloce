"""Response.add_etag + Response.make_conditional — conditional-response helpers."""

from __future__ import annotations

import pytest

from veloce import Request, Response
from veloce._internal import _etag_matches_strong
from veloce.exceptions import PreconditionFailed


def _req(headers: dict | None = None) -> Request:
    return Request(method="GET", path="/", query_string="", headers=headers or {}, body=b"")


# ── add_etag ─────────────────────────────────────────────────────────


def test_add_etag_sets_strong_quoted_etag():
    resp = Response(body=b"hello world")
    tag = resp.add_etag()
    # MD5 of "hello world".
    assert tag == '"5eb63bbbe01eeed093cb22bb8f5acdc3"'
    assert resp.headers["ETag"] == tag


def test_add_etag_passes_usedforsecurity_false(monkeypatch):
    """The cache-validator MD5 must be flagged non-security so it does not
    raise on FIPS Python builds."""
    import hashlib

    seen = {}
    real_md5 = hashlib.md5

    def _spy(data=b"", *, usedforsecurity=True):
        seen["flag"] = usedforsecurity
        return real_md5(data, usedforsecurity=usedforsecurity)

    monkeypatch.setattr(hashlib, "md5", _spy)
    resp = Response(body=b"hello world")
    assert resp.add_etag() == '"5eb63bbbe01eeed093cb22bb8f5acdc3"'  # digest unchanged
    assert seen["flag"] is False


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


# ── _etag_matches_strong (RFC 9110 §8.8.3.1) ─────────────────────────


def test_strong_compare_matches_identical_strong_tags():
    assert _etag_matches_strong('"abc"', '"abc"') is True


def test_strong_compare_rejects_weak_on_either_side():
    assert _etag_matches_strong('W/"abc"', '"abc"') is False
    assert _etag_matches_strong('"abc"', 'W/"abc"') is False
    assert _etag_matches_strong('W/"abc"', 'W/"abc"') is False


def test_strong_compare_handles_surrounding_whitespace():
    assert _etag_matches_strong('"abc"', '  "abc" ') is True
    assert _etag_matches_strong('"abc"', '"xyz"') is False


# ── check_preconditions: If-Match (RFC 9110 §13.1.1) ─────────────────


def test_check_preconditions_strong_match_returns_self():
    resp = Response(body=b"hello")
    resp.add_etag()
    req = _req({"if-match": resp.headers["ETag"]})
    assert resp.check_preconditions(req) is resp


def test_check_preconditions_strong_mismatch_raises_412():
    resp = Response(body=b"hello")
    resp.add_etag()
    req = _req({"if-match": '"nope"'})
    with pytest.raises(PreconditionFailed):
        resp.check_preconditions(req)


def test_check_preconditions_weak_validator_never_satisfies_if_match():
    """A weak ETag on the response can never satisfy If-Match (§8.8.3.1)."""
    resp = Response(body=b"hello")
    resp.add_etag(weak=True)
    # Both the weak form and the bare opaque form must fail.
    for token in (resp.headers["ETag"], '"' + resp.headers["ETag"][3:].strip('"') + '"'):
        req = _req({"if-match": token})
        with pytest.raises(PreconditionFailed):
            resp.check_preconditions(req)


def test_check_preconditions_wildcard_passes_when_etag_present():
    resp = Response(body=b"hello")
    resp.add_etag()
    req = _req({"if-match": "*"})
    assert resp.check_preconditions(req) is resp


def test_check_preconditions_wildcard_passes_without_etag():
    # `If-Match: *` is an existence precondition - a concrete response
    # satisfies it even when no ETag was attached (RFC 9110 Sec. 13.1.1).
    resp = Response(body=b"hello")
    req = _req({"if-match": "*"})
    assert resp.check_preconditions(req) is resp


def test_check_preconditions_absent_header_returns_self():
    resp = Response(body=b"hello")
    resp.add_etag()
    req = _req()
    assert resp.check_preconditions(req) is resp


def test_check_preconditions_multiple_tags_one_strong_match_passes():
    resp = Response(body=b"hello")
    tag = resp.add_etag()
    req = _req({"if-match": f'"other", {tag}'})
    assert resp.check_preconditions(req) is resp


def test_check_preconditions_accepts_lowercase_etag_key():
    # A response whose validator was set under the lowercase "etag" spelling
    # must still satisfy a matching If-Match (headers is a plain dict).
    resp = Response(body=b"hello")
    resp.headers["etag"] = '"abc"'
    assert resp.check_preconditions(_req({"if-match": '"abc"'})) is resp
    with pytest.raises(PreconditionFailed):
        resp.check_preconditions(_req({"if-match": '"nope"'}))


# ── A 304 advertises the representation length, not zero ────────────


def test_a_downgraded_304_advertises_the_representation_length():
    """The downgrade emptied the body before the length was computed.

    A 304 deliberately carries the Content-Length a 200 would have carried
    (RFC 9110 Sec. 8.6), which is what a directly-built 304 does. The downgrade
    path dropped the body first, so the length came out as 0 - and RFC 9111
    Sec. 4.3.4 has caches update their stored headers from the 304, writing
    that zero over the stored length of a resource that is not empty.
    """
    response = Response(body=b"x" * 48)
    response.headers["ETag"] = 'W/"abc"'
    response._downgrade_to_304()
    assert b"Content-Length: 48" in response.encode()


def test_a_downgraded_304_sends_no_body():
    response = Response(body=b"x" * 48)
    response._downgrade_to_304()
    assert response.encode().endswith(b"\r\n\r\n")


def test_a_downgraded_304_still_carries_the_validator():
    """Advertising the length must not disturb what the 304 exists to convey."""
    response = Response(body=b"x" * 48)
    response.headers["ETag"] = 'W/"abc"'
    response._downgrade_to_304()
    assert b'ETag: W/"abc"' in response.encode()


def test_a_downgraded_empty_representation_advertises_zero():
    """Zero is correct when the representation really is empty."""
    response = Response(body=b"")
    response._downgrade_to_304()
    assert b"Content-Length: 0" in response.encode()


def test_a_compressed_length_is_the_one_preserved():
    """The recorded length is whatever the body holds at downgrade time."""
    response = Response(body=b"compressed")
    response.headers["Content-Encoding"] = "gzip"
    response._downgrade_to_304()
    assert b"Content-Length: 10" in response.encode()


def test_a_200_still_advertises_its_length():
    response = Response(body=b"x" * 48)
    assert b"content-length: 48" in response.encode().lower()


def test_a_204_still_advertises_zero():
    """Only the downgrade path changed; a bodiless status keeps its zero."""
    response = Response(status_code=204)
    assert b"content-length: 0" in response.encode().lower()


def test_the_downgrade_is_idempotent():
    """A handler and a conditional-GET middleware may both call it.

    The second pass sees the emptied body, so recomputing replaced the recorded
    length with zero - which is how a fully-wired app still advertised
    `Content-Length: 0` after the length was being recorded correctly.
    """
    response = Response(body=b"x" * 226)
    response.headers["ETag"] = 'W/"a"'
    response._downgrade_to_304()
    response._downgrade_to_304()
    assert b"Content-Length: 226" in response.encode()


def test_a_second_make_conditional_keeps_the_length():
    request = _req({"if-none-match": 'W/"a"'})
    response = Response(body=b"x" * 226)
    response.headers["ETag"] = 'W/"a"'
    response.make_conditional(request)
    response.make_conditional(request)
    assert response.status_code == 304
    assert b"Content-Length: 226" in response.encode()
