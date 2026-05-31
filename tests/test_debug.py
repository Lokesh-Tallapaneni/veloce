"""Dev-only HTML traceback page (src/veloce/debug.py)."""

from __future__ import annotations

from veloce import Veloce
from veloce.debug import render_traceback_html
from veloce.testclient import TestClient


def _boom_app(message: str = "kaboom") -> Veloce:
    app = Veloce(debug=True)

    @app.get("/boom")
    async def boom():
        raise ValueError(message)

    return app


def test_debug_returns_html_traceback_with_source_context():
    app = _boom_app()
    with TestClient(app) as client:
        resp = client.get("/boom")

    assert resp.status_code == 500
    assert "text/html" in resp.content_type
    body = resp.text
    # Exception type and the failing frame's file are present.
    assert "ValueError" in body
    assert "test_debug.py" in body
    # The source-context window includes the raising line.
    assert "raise ValueError(message)" in body


def test_debug_html_escapes_exception_message():
    app = _boom_app("<script>alert(1)</script>")
    with TestClient(app) as client:
        resp = client.get("/boom")

    body = resp.text
    # The raw tag must not survive into the markup.
    assert "<script>alert(1)</script>" not in body
    # It must appear in escaped form instead.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_debug_false_returns_json_error_no_source_leak():
    app = Veloce(debug=False)

    @app.get("/boom")
    async def boom():
        raise ValueError("secret-internal-detail")

    with TestClient(app) as client:
        resp = client.get("/boom")

    assert resp.status_code == 500
    assert "application/json" in resp.content_type
    assert resp.json() == {"detail": "Internal Server Error"}
    # No traceback, no source, no exception message leaked.
    assert "secret-internal-detail" not in resp.text
    assert "ValueError" not in resp.text
    assert "<html" not in resp.text.lower()


def test_debug_page_has_no_interactive_or_eval_affordance():
    app = _boom_app()
    with TestClient(app) as client:
        resp = client.get("/boom")

    body = resp.text.lower()
    # No-eval contract: the page must not post back or execute code.
    assert "<form" not in body
    assert "<input" not in body
    assert "<script" not in body
    assert "<textarea" not in body
    assert "eval(" not in body


def test_render_traceback_html_is_self_contained_document():
    try:
        raise RuntimeError("standalone")
    except RuntimeError as exc:
        page = render_traceback_html(exc)

    assert page.startswith("<!doctype html>")
    assert "RuntimeError" in page
    assert "standalone" in page
    assert "<style>" in page
    # No scripts or forms in the rendered document.
    assert "<script" not in page.lower()
    assert "<form" not in page.lower()


def test_render_traceback_html_escapes_message():
    try:
        raise ValueError("<b>bold</b>")
    except ValueError as exc:
        page = render_traceback_html(exc)

    assert "<b>bold</b>" not in page
    assert "&lt;b&gt;bold&lt;/b&gt;" in page
