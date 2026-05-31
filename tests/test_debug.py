"""Dev-only HTML traceback page (src/veloce/debug.py)."""

from __future__ import annotations

import builtins
import sys

import pytest

from veloce import Veloce
from veloce.debug import render_traceback_html
from veloce.testclient import TestClient

# ExceptionGroup is a builtin from Python 3.11; reference it via ``builtins`` so
# the test module still imports cleanly under the project's 3.10 lint target.
_ExceptionGroup = getattr(builtins, "ExceptionGroup", None)


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


def test_render_traceback_html_survives_unprintable_exception():
    # An exception whose __str__ itself raises must not crash the renderer;
    # the stdlib traceback placeholder is emitted instead of a second error.
    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str() blew up")

    try:
        raise Unprintable()
    except Unprintable as exc:
        page = render_traceback_html(exc)

    assert page.startswith("<!doctype html>")
    assert "Unprintable" in page
    assert "&lt;exception str() failed&gt;" in page


def test_render_traceback_html_includes_explicit_cause_chain():
    # raise ... from ... must surface both exceptions and the cause separator.
    try:
        try:
            raise ValueError("the-inner-cause")
        except ValueError as inner:
            raise RuntimeError("the-outer-error") from inner
    except RuntimeError as exc:
        page = render_traceback_html(exc)

    assert "the-inner-cause" in page
    assert "the-outer-error" in page
    assert "The above exception was the direct cause" in page
    # The cause is rendered before the outer error within the body.
    body = page[page.index("<main>") :]
    assert body.index("the-inner-cause") < body.index("the-outer-error")


def test_render_traceback_html_includes_implicit_context_chain():
    # An exception raised while handling another keeps the context separator.
    try:
        try:
            raise KeyError("first-failure")
        except KeyError:
            raise TypeError("second-failure")
    except TypeError as exc:
        page = render_traceback_html(exc)

    assert "first-failure" in page
    assert "second-failure" in page
    assert "During handling of the above exception" in page


def test_render_traceback_html_renders_exception_notes():
    # BaseException.add_note() content (PEP 678) appears in the output.
    try:
        raise ValueError("with-notes")
    except ValueError as exc:
        exc.add_note("first added note")
        exc.add_note("second added note")
        page = render_traceback_html(exc)

    assert "first added note" in page
    assert "second added note" in page


def test_render_traceback_html_escapes_notes():
    try:
        raise ValueError("noted")
    except ValueError as exc:
        exc.add_note("<i>note-markup</i>")
        page = render_traceback_html(exc)

    assert "<i>note-markup</i>" not in page
    assert "&lt;i&gt;note-markup&lt;/i&gt;" in page


def test_render_traceback_html_includes_syntax_error_text_and_caret():
    # SyntaxError carries the failing source on exc.text/exc.offset, which the
    # frame-only walk never surfaces. The renderer must reproduce the offending
    # line and a caret under the failing column, like the stdlib traceback.
    try:
        compile("x =\n", "bad.py", "exec")
    except SyntaxError as exc:
        page = render_traceback_html(exc)

    assert "SyntaxError" in page
    # The offending source line is shown verbatim.
    assert "x =" in page
    # A caret marker points at the failing column.
    assert "^" in page
    assert 'class="syntax"' in page


def test_render_traceback_html_handles_indentation_error():
    try:
        compile("def f():\npass\n", "bad.py", "exec")
    except (IndentationError, SyntaxError) as exc:
        page = render_traceback_html(exc)

    # IndentationError is a SyntaxError subclass; its source/caret render too.
    assert "Error" in page
    assert "^" in page


def test_render_traceback_html_escapes_syntax_error_source():
    # The offending source line is user-controlled and must be HTML-escaped.
    try:
        compile("<tag> = 1\n", "bad.py", "exec")
    except SyntaxError as exc:
        page = render_traceback_html(exc)

    assert "<tag>" not in page.replace("&lt;tag&gt;", "")
    assert "&lt;tag&gt;" in page


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup requires Python 3.11+")
def test_render_traceback_html_descends_into_exception_group():
    # PEP 654 groups (e.g. asyncio.TaskGroup failures) carry child exceptions on
    # .exceptions, not on __cause__/__context__. The renderer must descend into
    # them so nested failures are not silently dropped.
    eg = _ExceptionGroup(
        "group-wrapper",
        [ValueError("first-child-error"), KeyError("second-child-error")],
    )
    page = render_traceback_html(eg)

    assert "group-wrapper" in page
    assert "first-child-error" in page
    assert "second-child-error" in page
    assert 'class="group"' in page


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup requires Python 3.11+")
def test_render_traceback_html_descends_into_nested_exception_groups():
    inner = _ExceptionGroup("inner-group", [RuntimeError("deep-error")])
    outer = _ExceptionGroup("outer-group", [inner, TypeError("shallow-error")])
    page = render_traceback_html(outer)

    assert "outer-group" in page
    assert "inner-group" in page
    assert "deep-error" in page
    assert "shallow-error" in page


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup requires Python 3.11+")
def test_render_traceback_html_renders_chained_cause_within_group_child():
    try:
        try:
            raise ValueError("root-cause")
        except ValueError as inner:
            raise RuntimeError("wrapped-error") from inner
    except RuntimeError as exc:
        eg = _ExceptionGroup("grouped", [exc])
        page = render_traceback_html(eg)

    assert "grouped" in page
    assert "root-cause" in page
    assert "wrapped-error" in page
    assert "The above exception was the direct cause" in page
