"""render_template + render_template_string module helpers."""

from __future__ import annotations

import pytest

from tests._templating import install_templates, templates_at
from veloce import StreamingResponse, Veloce
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
    (tmp_path / "ignored.html").write_text("ignored")
    app = Veloce(openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))
    install_templates(app, templates)

    with app.app_context():
        out = render_template_string("X={{ x }}", x=7)
        assert out == "X=7"


# ── render_template ──────────────────────────────────────────────────


def test_render_template_outside_app_raises():
    with pytest.raises(RuntimeError, match="active application"):
        render_template("anything.html")


def test_render_template_without_templates_attr_raises():
    app = Veloce(openapi_url=None)
    with app.app_context(), pytest.raises(RuntimeError, match="template_folder"):
        render_template("x.html")


def test_render_template_renders_named_file(tmp_path):
    (tmp_path / "hello.html").write_text("Hi {{ name }}!")
    app = Veloce(openapi_url=None)
    templates_at(app, str(tmp_path))

    with app.app_context():
        assert render_template("hello.html", name="alice") == "Hi alice!"


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
    app = Veloce(openapi_url=None)
    with app.app_context(), pytest.raises(RuntimeError, match="template_folder"):
        stream_template("x.html")


def test_stream_template_streams_named_file(tmp_path):
    (tmp_path / "items.html").write_text("{% for i in items %}<li>{{ i }}</li>{% endfor %}")
    app = Veloce(openapi_url=None)
    templates_at(app, str(tmp_path))

    with app.app_context():
        out = "".join(stream_template("items.html", items=["a", "b"]))
        assert out == "<li>a</li><li>b</li>"


def test_stream_template_wraps_in_streaming_response(tmp_path):
    """The generator is consumable by StreamingResponse."""

    (tmp_path / "page.html").write_text("Hello {{ who }}")
    app = Veloce(openapi_url=None)
    templates_at(app, str(tmp_path))

    with app.app_context():
        resp = StreamingResponse(stream_template("page.html", who="world"))
        assert resp.is_streamed


def test_stream_template_resolves_context_when_consumed_after_request(tmp_path):
    """A streamed template that reads a context-dependent global (`url_for`)
    must render correctly when its body is consumed AFTER the request context
    is gone — the built-in server emits the body on a separate task.

    The stream is built inside the app context, the context is then torn down,
    and only then is the (synchronous) body iterated — it must not raise
    "working outside of application context".
    """
    # `url_for` is injected as a Jinja global by _sync_app_jinja_helpers and
    # resolves lazily during iteration — the exact context-dependent case.
    (tmp_path / "ctx.html").write_text(
        "{% for i in items %}{{ url_for('home') }}:{{ i }};{% endfor %}"
    )

    app = Veloce(openapi_url=None)
    templates_at(app, str(tmp_path))

    @app.get("/", name="home")
    async def home(request):
        return {"ok": True}

    with app.app_context():
        gen = stream_template("ctx.html", items=["a", "b"])

    # Context is gone now. Consuming the iterator must still resolve url_for
    # via the snapshot captured when the stream was built.
    out = "".join(gen)
    assert out == "/:a;/:b;"
