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
        # A browser-like Accept selects the HTML traceback view.
        resp = client.get("/boom", headers={"accept": "text/html"})

    assert resp.status_code == 500
    assert "text/html" in resp.content_type
    body = resp.text
    # Exception type and the failing frame's file are present.
    assert "ValueError" in body
    assert "test_debug.py" in body
    # The source-context window includes the raising line.
    assert "raise ValueError(message)" in body


def test_debug_serves_plaintext_traceback_to_non_html_clients():
    # A curl / CLI / programmatic client (no Accept, or not preferring HTML)
    # keeps the plain-text traceback it got before the HTML page existed —
    # the debug Content-Type contract is unchanged for them.
    app = _boom_app()
    with TestClient(app) as client:
        no_accept = client.get("/boom")
        star = client.get("/boom", headers={"accept": "*/*"})
        plain = client.get("/boom", headers={"accept": "text/plain"})

    for resp in (no_accept, star, plain):
        assert resp.status_code == 500
        assert "text/plain" in resp.content_type
        assert "text/html" not in resp.content_type
        # Still a real traceback, just not the HTML view.
        assert "ValueError" in resp.text
        assert "Traceback" in resp.text
        # No HTML markup leaked into the plain-text body.
        assert "<html" not in resp.text.lower()


def test_debug_html_escapes_exception_message():
    app = _boom_app("<script>alert(1)</script>")
    with TestClient(app) as client:
        resp = client.get("/boom", headers={"accept": "text/html"})

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
    """The served HTML page offers nothing to post back or execute.

    This asked for `/boom` with no `Accept` header, which returns the
    **plain-text** traceback - so every `"<form" not in body` assertion held
    trivially and the HTML page named in the test's own title was never
    fetched. `Accept: text/html` is what selects it.
    """
    app = _boom_app()
    with TestClient(app) as client:
        resp = client.get("/boom", headers={"Accept": "text/html"})

    body = resp.text.lower()
    # The premise: assert we are looking at the HTML page at all, so this
    # cannot quietly revert to checking the plain-text body again.
    assert "text/html" in resp.headers["content-type"]
    assert "<html" in body

    # No-eval contract: the page must not post back or execute code.
    assert "<form" not in body
    assert "<input" not in body
    assert "<script" not in body
    assert "<textarea" not in body
    assert "eval(" not in body
    assert "onclick" not in body


def test_the_plain_text_traceback_is_served_without_an_html_accept():
    """The other branch, which is what the test above used to exercise."""
    app = _boom_app()
    with TestClient(app) as client:
        resp = client.get("/boom")

    assert "text/plain" in resp.headers["content-type"]
    assert "<html" not in resp.text.lower()


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
    # PEP 678 exception notes appear in the output. Set ``__notes__`` directly
    # (the attribute the renderer reads) rather than via ``add_note()`` so the
    # test runs on Python 3.10, where ``add_note()`` does not yet exist.
    try:
        raise ValueError("with-notes")
    except ValueError as exc:
        exc.__notes__ = ["first added note", "second added note"]
        page = render_traceback_html(exc)

    assert "first added note" in page
    assert "second added note" in page


def test_render_traceback_html_escapes_notes():
    try:
        raise ValueError("noted")
    except ValueError as exc:
        exc.__notes__ = ["<i>note-markup</i>"]
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


# ── `debug` is bound to config["DEBUG"] ───────────────────────
#
# Moved here from `test_app.py`, where these sat in a bare-function tail whose
# sections were labelled by internal batch id (`S7:`, `P-6:`).


def test_debug_attr_writes_config():

    app = Veloce(openapi_url=None)
    app.debug = True
    assert app.config["DEBUG"] is True


def test_config_debug_reflected_in_attr():

    app = Veloce(openapi_url=None)
    app.config["DEBUG"] = True
    assert app.debug is True


def test_debug_constructor_seeds_config():

    assert Veloce(debug=True, openapi_url=None).config["DEBUG"] is True
    assert Veloce(openapi_url=None).config["DEBUG"] is False


def test_post_construction_debug_enables_html_traceback():
    app = Veloce(openapi_url=None)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    app.config["DEBUG"] = True  # flip AFTER construction
    with TestClient(app) as client:
        resp = client.get("/boom", headers={"accept": "text/html"})
    # Flipping config["DEBUG"] after construction now serves the HTML debug
    # traceback page (the path that reads self.debug, now bound to the config
    # key) instead of the production JSON error.
    assert resp.status_code == 500
    assert "text/html" in resp.content_type
    assert "RuntimeError" in resp.text


def test_debug_string_false_is_falsey():
    # A dotenv-loaded `DEBUG=false` is the string "false"; it must read as False,
    # not truthy. Guards the bool("false") regression on string-based config.

    app = Veloce(openapi_url=None)
    app.config["DEBUG"] = "false"
    assert app.debug is False
    app.config["DEBUG"] = "true"
    assert app.debug is True


def test_debug_setter_coerces_string():
    # `app.debug = "false"` (string from an env source) must store False.

    app = Veloce(openapi_url=None)
    app.debug = "false"
    assert app.debug is False and app.config["DEBUG"] is False
    app.debug = "true"
    assert app.debug is True


def test_run_rejects_multiple_workers():
    """The built-in server is single-process; run(workers>1) fails loudly."""
    app = Veloce()
    with pytest.raises(ValueError, match="runs a single process"):
        app.run(workers=4)


def test_app_still_works():
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index():
        return {"ok": True}

    assert app.test_client().get("/").json() == {"ok": True}
