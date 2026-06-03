"""Fallback-list template resolution for `Jinja2Templates` (first-existing)."""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

from veloce import Veloce
from veloce.contrib.templating import Jinja2Templates, render_template
from veloce.testclient import TestClient


@pytest.fixture
def tmpl_dir(tmp_path: Path) -> Path:
    return tmp_path


def test_fallback_list_picks_first_existing(tmpl_dir: Path):
    (tmpl_dir / "base.html").write_text("BASE {{ name }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.get("/")
    async def index():
        return templates.TemplateResponse(["theme/page.html", "base.html"], {"name": "x"})

    resp = TestClient(app).get("/")
    assert resp.body == b"BASE x"


def test_fallback_list_prefers_earlier_candidate(tmpl_dir: Path):
    (tmpl_dir / "theme").mkdir()
    (tmpl_dir / "theme" / "page.html").write_text("THEME")
    (tmpl_dir / "base.html").write_text("BASE")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.get("/")
    async def index():
        return templates.TemplateResponse(["theme/page.html", "base.html"], {})

    resp = TestClient(app).get("/")
    assert resp.body == b"THEME"


def test_fallback_list_all_missing_raises(tmpl_dir: Path):
    templates = Jinja2Templates(directory=str(tmpl_dir))
    with pytest.raises(jinja2.TemplateNotFound):
        templates.render(["a.html", "b.html"])


def test_single_string_unchanged(tmpl_dir: Path):
    (tmpl_dir / "x.html").write_text("ONE {{ v }}")
    templates = Jinja2Templates(directory=str(tmpl_dir))
    assert templates.render("x.html", {"v": "y"}) == "ONE y"


def test_render_module_helper_accepts_list(tmpl_dir: Path):
    (tmpl_dir / "y.html").write_text("Y-HELPER")
    app = Veloce(debug=True, openapi_url=None)
    app._templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.get("/")
    async def index():
        return render_template(["x.html", "y.html"])

    resp = TestClient(app).get("/")
    assert resp.body == b"Y-HELPER"


def test_fallback_cache_when_auto_reload_off(tmpl_dir: Path):
    (tmpl_dir / "base.html").write_text("BASE")
    templates = Jinja2Templates(directory=str(tmpl_dir), auto_reload=False)

    first = templates.render(["theme/page.html", "base.html"])
    assert first == "BASE"
    # The winning name is now memoized keyed on (id(env), candidates).
    key = (id(templates.env), ("theme/page.html", "base.html"))
    assert templates._resolved_cache[key] == "base.html"

    second = templates.render(["theme/page.html", "base.html"])
    assert second == "BASE"
