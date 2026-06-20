"""PlainTextResponse and HTMLResponse accept both str and bytes (#216)."""

from __future__ import annotations

from veloce import HTMLResponse, PlainTextResponse


def test_plain_text_response_with_str():
    resp = PlainTextResponse(content="hello")
    assert resp.body == b"hello"
    assert resp.status_code == 200


def test_plain_text_response_with_bytes():
    resp = PlainTextResponse(content=b"hello")
    assert resp.body == b"hello"
    assert resp.status_code == 200


def test_html_response_with_str():
    resp = HTMLResponse(content="<h1>hi</h1>")
    assert resp.body == b"<h1>hi</h1>"
    assert resp.status_code == 200


def test_html_response_with_bytes():
    resp = HTMLResponse(content=b"<h1>hi</h1>")
    assert resp.body == b"<h1>hi</h1>"
    assert resp.status_code == 200
