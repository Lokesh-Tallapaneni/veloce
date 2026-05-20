"""Response.status full status-line accessor."""

from __future__ import annotations

from veloce import Response


def test_status_default_200_ok():
    assert Response().status == "200 OK"


def test_status_reflects_status_code():
    assert Response(status_code=404).status == "404 Not Found"
    assert Response(status_code=201).status == "201 Created"


def test_status_setter_from_int():
    resp = Response()
    resp.status = 503
    assert resp.status_code == 503
    assert resp.status == "503 Service Unavailable"


def test_status_setter_from_bare_numeric_string():
    resp = Response()
    resp.status = "418"
    assert resp.status_code == 418


def test_status_setter_from_full_status_line():
    resp = Response()
    resp.status = "404 Not Found"
    assert resp.status_code == 404


def test_status_setter_from_custom_phrase():
    resp = Response()
    resp.status = "200 Totally Fine"
    # Only the leading int is parsed.
    assert resp.status_code == 200


def test_status_unknown_code_has_no_phrase():
    resp = Response(status_code=299)
    # 299 is not a registered code — status line is just the number.
    assert resp.status == "299"
