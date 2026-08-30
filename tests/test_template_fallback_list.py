"""Fallback-list template resolution for `Jinja2Templates` (first-existing)."""

from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

from veloce import Veloce
from veloce.contrib.templating import Jinja2Templates, render_template
from veloce.testclient import TestClient


def test_fallback_list_picks_first_existing(tmp_path: Path):
    (tmp_path / "base.html").write_text("BASE {{ name }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/")
    async def index():
        return templates.TemplateResponse(["theme/page.html", "base.html"], {"name": "x"})

    resp = TestClient(app).get("/")
    assert resp.body == b"BASE x"


def test_fallback_list_prefers_earlier_candidate(tmp_path: Path):
    (tmp_path / "theme").mkdir()
    (tmp_path / "theme" / "page.html").write_text("THEME")
    (tmp_path / "base.html").write_text("BASE")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/")
    async def index():
        return templates.TemplateResponse(["theme/page.html", "base.html"], {})

    resp = TestClient(app).get("/")
    assert resp.body == b"THEME"


def test_fallback_list_all_missing_raises(tmp_path: Path):
    templates = Jinja2Templates(directory=str(tmp_path))
    with pytest.raises(jinja2.TemplateNotFound):
        templates.render(["a.html", "b.html"])


def test_single_string_unchanged(tmp_path: Path):
    (tmp_path / "x.html").write_text("ONE {{ v }}")
    templates = Jinja2Templates(directory=str(tmp_path))
    assert templates.render("x.html", {"v": "y"}) == "ONE y"


def test_render_module_helper_accepts_list(tmp_path: Path):
    (tmp_path / "y.html").write_text("Y-HELPER")
    app = Veloce(debug=True, openapi_url=None)
    app._templates = Jinja2Templates(directory=str(tmp_path))

    @app.get("/")
    async def index():
        return render_template(["x.html", "y.html"])

    resp = TestClient(app).get("/")
    assert resp.body == b"Y-HELPER"


def test_fallback_cache_when_auto_reload_off(tmp_path: Path):
    (tmp_path / "base.html").write_text("BASE")
    templates = Jinja2Templates(directory=str(tmp_path), auto_reload=False)

    first = templates.render(["theme/page.html", "base.html"])
    assert first == "BASE"
    # The winning name is now memoized keyed on (id(env), candidates).
    key = (id(templates.env), ("theme/page.html", "base.html"))
    assert templates._resolved_cache[key] == "base.html"

    second = templates.render(["theme/page.html", "base.html"])
    assert second == "BASE"


def test_fallback_cache_evicts_oldest_at_cap(tmp_path: Path):
    (tmp_path / "base.html").write_text("BASE")
    templates = Jinja2Templates(directory=str(tmp_path), auto_reload=False)
    templates.RESOLVED_CACHE_MAX = 3

    # Each candidate list has a distinct (non-existent) leading name, so every
    # call is a new cache key but all resolve to `base.html`.
    def first_key() -> tuple[int, tuple[str, ...]]:
        return next(iter(templates._resolved_cache))

    templates.render(["miss-0.html", "base.html"])
    templates.render(["miss-1.html", "base.html"])
    templates.render(["miss-2.html", "base.html"])
    assert len(templates._resolved_cache) == 3
    oldest = (id(templates.env), ("miss-0.html", "base.html"))
    assert first_key() == oldest

    # The fourth distinct key is at the cap: it evicts the oldest key and the
    # dict stays bounded.
    templates.render(["miss-3.html", "base.html"])
    assert len(templates._resolved_cache) == 3
    assert oldest not in templates._resolved_cache
    assert (id(templates.env), ("miss-3.html", "base.html")) in templates._resolved_cache


def test_resolved_cache_cap_zero_does_not_crash(tmp_path: Path):
    """`RESOLVED_CACHE_MAX <= 0` disables the resolution cache rather than
    raising `StopIteration` on the first insert (cap 0 is a natural "off" value).
    """
    (tmp_path / "base.html").write_text("BASE {{ name }}")
    templates = Jinja2Templates(directory=str(tmp_path), auto_reload=False)
    templates.RESOLVED_CACHE_MAX = 0
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/")
    async def index():
        return templates.TemplateResponse(["theme/page.html", "base.html"], {"name": "x"})

    client = TestClient(app)
    assert client.get("/").body == b"BASE x"
    # Second hit re-enters the (disabled) cache path; must not crash and must
    # cache nothing at cap 0.
    assert client.get("/").body == b"BASE x"
    assert len(templates._resolved_cache) == 0
