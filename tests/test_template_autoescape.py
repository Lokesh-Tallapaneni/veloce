"""Default Jinja autoescape covers HTML-shaped extensions (TP3 polish)."""

from __future__ import annotations

from pathlib import Path

from veloce import Veloce
from veloce.contrib.templating import Jinja2Templates
from veloce.testclient import TestClient


def test_html_template_escapes_user_input(tmp_path: Path):
    (tmp_path / "p.html").write_text("Hi {{ name }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/")
    async def index():
        return templates.TemplateResponse("p.html", {"name": "<script>"})

    resp = TestClient(app).get("/")
    assert resp.body == b"Hi &lt;script&gt;"


def test_xml_template_also_escapes(tmp_path: Path):
    (tmp_path / "feed.xml").write_text("<title>{{ title }}</title>")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/")
    async def index():
        return templates.TemplateResponse("feed.xml", {"title": "<x>"})

    resp = TestClient(app).get("/")
    assert resp.body == b"<title>&lt;x&gt;</title>"


def test_txt_template_does_not_escape(tmp_path: Path):
    """Plain-text templates have no XSS risk; autoescape is off."""
    (tmp_path / "p.txt").write_text("Hi {{ name }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/")
    async def index():
        return templates.TemplateResponse("p.txt", {"name": "<script>"})

    resp = TestClient(app).get("/")
    assert resp.body == b"Hi <script>"


def test_autoescape_override_disables_globally(tmp_path: Path):
    """`autoescape=False` in the ctor disables escaping even for .html."""
    (tmp_path / "p.html").write_text("Hi {{ name }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path), autoescape=False)

    @app.get("/")
    async def index():
        return templates.TemplateResponse("p.html", {"name": "<script>"})

    resp = TestClient(app).get("/")
    assert resp.body == b"Hi <script>"


def test_safe_filter_opts_out(tmp_path: Path):
    """`{{ x | safe }}` skips autoescape for that one expression."""
    (tmp_path / "p.html").write_text("{{ raw|safe }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/")
    async def index():
        return templates.TemplateResponse("p.html", {"raw": "<b>bold</b>"})

    resp = TestClient(app).get("/")
    assert resp.body == b"<b>bold</b>"


# ── auto_reload tracks the app's debug flag (P-4) ────────────────────


def test_auto_reload_tracks_app_debug(tmp_path: Path):
    """With `auto_reload` left unset, it follows the bound app's `debug`:
    off in production (no per-render template stat), on in development."""
    templates = Jinja2Templates(directory=str(tmp_path))

    prod = Veloce(openapi_url=None)  # debug defaults to False
    with prod.app_context():
        templates._apply_auto_reload(templates.env)
        assert templates.env.auto_reload is False

    dev = Veloce(debug=True, openapi_url=None)
    with dev.app_context():
        templates._apply_auto_reload(templates.env)
        assert templates.env.auto_reload is True


def test_explicit_auto_reload_is_respected(tmp_path: Path):
    """An explicit `auto_reload=` is never overridden by the app's debug."""
    templates = Jinja2Templates(directory=str(tmp_path), auto_reload=False)
    assert templates.env.auto_reload is False

    dev = Veloce(debug=True, openapi_url=None)
    with dev.app_context():
        templates._apply_auto_reload(templates.env)
        assert templates.env.auto_reload is False  # explicit wins over debug
