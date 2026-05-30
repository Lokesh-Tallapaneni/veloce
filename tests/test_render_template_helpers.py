"""render_template + render_template_string module helpers."""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.templating import (
    Jinja2Templates,
    render_template,
    render_template_string,
    stream_template,
)

# ── render_template_string ───────────────────────────────────────────


def test_render_template_string_outside_app_context_works():
    """Inline string templates render even without an app bound."""
    rendered = render_template_string("Hello {{ name }}!", name="alice")
    assert rendered == "Hello alice!"


def test_render_template_string_autoescapes_html_by_default():
    """The fallback env uses select_autoescape for html/htm/xml/xhtml; modern
    Jinja2 also enables autoescape for from_string templates by default,
    which keeps the helper XSS-safe out of the box."""
    out = render_template_string("{{ value }}", value="<b>")
    assert out == "&lt;b&gt;"


def test_render_template_string_inside_app_uses_app_templates(tmp_path):
    """When the app has `_templates` bound, the helper goes through it
    (so filters/globals/context processors apply)."""
    from veloce.helpers import _current_app_var

    (tmp_path / "ignored.html").write_text("ignored")
    app = Veloce(openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))
    app._templates = templates

    # Bind the app so the helper finds it.
    token = _current_app_var.set(app)
    try:
        out = render_template_string("X={{ x }}", x=7)
        assert out == "X=7"
    finally:
        _current_app_var.reset(token)


# ── render_template ──────────────────────────────────────────────────


def test_render_template_outside_app_raises():
    with pytest.raises(RuntimeError, match="active application"):
        render_template("anything.html")


def test_render_template_without_templates_attr_raises():
    from veloce.helpers import _current_app_var

    app = Veloce(openapi_url=None)
    token = _current_app_var.set(app)
    try:
        with pytest.raises(RuntimeError, match="Jinja2Templates"):
            render_template("x.html")
    finally:
        _current_app_var.reset(token)


def test_render_template_renders_named_file(tmp_path):
    from veloce.helpers import _current_app_var

    (tmp_path / "hello.html").write_text("Hi {{ name }}!")
    app = Veloce(openapi_url=None)
    app._templates = Jinja2Templates(directory=str(tmp_path))

    token = _current_app_var.set(app)
    try:
        assert render_template("hello.html", name="alice") == "Hi alice!"
    finally:
        _current_app_var.reset(token)


# ── stream_template ───────────────────────────────────────────────────


def test_stream_method_yields_chunks(tmp_path):
    (tmp_path / "loop.html").write_text("{% for n in nums %}{{ n }}{% endfor %}")
    templates = Jinja2Templates(directory=str(tmp_path))
    chunks = list(templates.stream("loop.html", {"nums": [1, 2, 3]}))
    # Jinja yields multiple chunks; the joined output is the full render.
    assert "".join(chunks) == "123"


def test_stream_template_outside_app_raises():
    with pytest.raises(RuntimeError, match="active application"):
        list(stream_template("anything.html"))


def test_stream_template_without_templates_attr_raises():
    from veloce.helpers import _current_app_var

    app = Veloce(openapi_url=None)
    token = _current_app_var.set(app)
    try:
        with pytest.raises(RuntimeError, match="Jinja2Templates"):
            stream_template("x.html")
    finally:
        _current_app_var.reset(token)


def test_stream_template_streams_named_file(tmp_path):
    from veloce.helpers import _current_app_var

    (tmp_path / "items.html").write_text("{% for i in items %}<li>{{ i }}</li>{% endfor %}")
    app = Veloce(openapi_url=None)
    app._templates = Jinja2Templates(directory=str(tmp_path))

    token = _current_app_var.set(app)
    try:
        out = "".join(stream_template("items.html", items=["a", "b"]))
        assert out == "<li>a</li><li>b</li>"
    finally:
        _current_app_var.reset(token)


def test_stream_template_wraps_in_streaming_response(tmp_path):
    """The generator is consumable by StreamingResponse."""
    from veloce import StreamingResponse
    from veloce.helpers import _current_app_var

    (tmp_path / "page.html").write_text("Hello {{ who }}")
    app = Veloce(openapi_url=None)
    app._templates = Jinja2Templates(directory=str(tmp_path))

    token = _current_app_var.set(app)
    try:
        resp = StreamingResponse(stream_template("page.html", who="world"))
        assert resp.is_streamed
    finally:
        _current_app_var.reset(token)
