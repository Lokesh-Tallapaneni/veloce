"""Response.set_data + Response.data property."""

from __future__ import annotations

from veloce import Response


def test_data_property_reads_body():
    resp = Response(body=b"hello")
    assert resp.data == b"hello"


def test_data_setter_replaces_body():
    resp = Response(body=b"hello")
    resp.data = b"world"
    assert resp.body == b"world"


def test_set_data_accepts_str_encoded_utf8():
    resp = Response()
    resp.set_data("café")
    assert resp.body == "café".encode()


def test_set_data_invalidates_cached_encode():
    resp = Response(body=b"first")
    resp.encode()  # populate `_encoded`
    assert resp._encoded is not None
    resp.set_data(b"second")
    assert resp._encoded is None


def test_set_data_refreshes_content_length_when_present():
    resp = Response(body=b"first")
    resp.headers["Content-Length"] = "5"
    resp.set_data(b"a longer body")
    assert resp.headers["Content-Length"] == str(len(b"a longer body"))


def test_set_data_leaves_content_length_alone_when_absent():
    """If the caller never set Content-Length, we don't introduce it."""
    resp = Response(body=b"x")
    resp.set_data(b"yy")
    assert "Content-Length" not in resp.headers
    assert "content-length" not in resp.headers


def test_data_str_assignment_also_works():
    """Property setter form: `resp.data = "string"`."""
    resp = Response()
    resp.data = "from-string"
    assert resp.body == b"from-string"
