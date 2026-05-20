"""Jinja filter/global/test decorator tests (TP5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from veloce import Veloce
from veloce.contrib.templating import Jinja2Templates
from veloce.testclient import TestClient


@pytest.fixture
def tmpl_dir(tmp_path: Path) -> Path:
    return tmp_path


# ── @template_filter ───────────────────────────────────────────────────


def test_template_filter_registered_by_function_name(tmpl_dir: Path):
    (tmpl_dir / "x.html").write_text("{{ name|shout }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.template_filter()
    def shout(s: str) -> str:
        return s.upper() + "!"

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {"name": "alice"})

    resp = TestClient(app).get("/")
    assert resp.body == b"ALICE!"


def test_template_filter_explicit_name(tmpl_dir: Path):
    (tmpl_dir / "x.html").write_text("{{ s|reversed_str }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.template_filter("reversed_str")
    def reverse_helper(s: str) -> str:
        return s[::-1]

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {"s": "hello"})

    resp = TestClient(app).get("/")
    assert resp.body == b"olleh"


# ── @template_global ───────────────────────────────────────────────────


def test_template_global_callable_in_templates(tmpl_dir: Path):
    (tmpl_dir / "x.html").write_text("{{ now() }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.template_global()
    def now() -> str:
        return "<TIME>"

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {})

    resp = TestClient(app).get("/")
    # `.html` triggers autoescape (Jinja `select_autoescape` default) so
    # the global callable's `<TIME>` return is HTML-escaped.
    assert resp.body == b"&lt;TIME&gt;"


def test_add_template_global_imperative(tmpl_dir: Path):
    (tmpl_dir / "x.html").write_text("{{ greet('alice') }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    def greet(who: str) -> str:
        return f"Hi {who}"

    app.add_template_global(greet)

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {})

    resp = TestClient(app).get("/")
    assert resp.body == b"Hi alice"


# ── @template_test ─────────────────────────────────────────────────────


def test_template_test_usable_in_if_expressions(tmpl_dir: Path):
    (tmpl_dir / "x.html").write_text("{% if n is even %}yes{% else %}no{% endif %}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.template_test("even")
    def is_even(n: int) -> bool:
        return n % 2 == 0

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {"n": 4})

    resp = TestClient(app).get("/")
    assert resp.body == b"yes"


# ── Idempotency / interaction with context_processor ──────────────────


def test_filter_and_context_processor_coexist(tmpl_dir: Path):
    (tmpl_dir / "x.html").write_text("{{ name|upper_full }} from {{ brand }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.template_filter()
    def upper_full(s: str) -> str:
        return s.upper()

    @app.context_processor
    def brand_proc():
        return {"brand": "Veloce"}

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {"name": "alice"})

    resp = TestClient(app).get("/")
    assert resp.body == b"ALICE from Veloce"


def test_multiple_renders_sync_idempotently(tmpl_dir: Path):
    """Calling render twice doesn't multiply or break filter registration."""
    (tmpl_dir / "x.html").write_text("{{ x|tag }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.template_filter()
    def tag(s: str) -> str:
        return f"[{s}]"

    @app.get("/x")
    async def index():
        return templates.TemplateResponse("x.html", {"x": "ok"})

    client = TestClient(app)
    r1 = client.get("/x")
    r2 = client.get("/x")
    assert r1.body == r2.body == b"[ok]"
