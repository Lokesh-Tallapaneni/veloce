"""@app.context_processor invoked by templating (TP6)."""

from __future__ import annotations

from pathlib import Path

import pytest

from veloce import Veloce
from veloce.contrib.templating import Jinja2Templates


@pytest.fixture
def tmpl_dir(tmp_path: Path) -> Path:
    (tmp_path / "hello.html").write_text("Hello, {{ name }} ({{ flavor }})!")
    return tmp_path


def test_context_processor_merged_into_template_context(tmpl_dir: Path):
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.context_processor
    def add_flavor():
        return {"flavor": "vanilla"}

    @app.get("/")
    async def index():
        return templates.TemplateResponse("hello.html", {"name": "alice"})

    from veloce.testclient import TestClient

    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert resp.body == b"Hello, alice (vanilla)!"


def test_explicit_context_wins_over_processor(tmpl_dir: Path):
    """When a context_processor returns `flavor=A` and the handler passes
    `flavor=B`, the handler's value wins."""
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.context_processor
    def add_flavor():
        return {"flavor": "DEFAULT"}

    @app.get("/")
    async def index():
        return templates.TemplateResponse("hello.html", {"name": "alice", "flavor": "explicit"})

    from veloce.testclient import TestClient

    resp = TestClient(app).get("/")
    assert b"explicit" in resp.body
    assert b"DEFAULT" not in resp.body


def test_multiple_context_processors_all_merge(tmpl_dir: Path):
    (tmpl_dir / "multi.html").write_text("{{ a }} {{ b }} {{ c }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.context_processor
    def one():
        return {"a": "alpha"}

    @app.context_processor
    def two():
        return {"b": "beta"}

    @app.get("/")
    async def index():
        return templates.TemplateResponse("multi.html", {"c": "gamma"})

    from veloce.testclient import TestClient

    resp = TestClient(app).get("/")
    assert resp.body == b"alpha beta gamma"


def test_no_app_context_renders_with_just_caller_context(tmpl_dir: Path):
    """Rendering outside a request scope (no current_app) still works:
    the call uses only the caller's explicit context."""
    templates = Jinja2Templates(directory=str(tmpl_dir))
    (tmpl_dir / "x.html").write_text("hi {{ who }}")
    html = templates.render_string("hi {{ who }}", {"who": "world"})
    assert html == "hi world"


def test_async_context_processor_skipped_in_sync_template_path(tmpl_dir: Path):
    """An async context_processor can't be awaited from the sync render
    path. Veloce skips it without raising — the handler still gets
    a usable render."""
    (tmpl_dir / "sync.html").write_text("{{ greeting }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.context_processor
    async def async_proc():
        return {"greeting": "from-async"}

    @app.context_processor
    def sync_proc():
        return {"greeting": "from-sync"}

    @app.get("/")
    async def index():
        return templates.TemplateResponse("sync.html", {})

    from veloce.testclient import TestClient

    resp = TestClient(app).get("/")
    # Async processor was skipped; sync processor's value is used.
    assert resp.body == b"from-sync"


def test_context_processor_returning_non_dict_ignored(tmpl_dir: Path):
    """A processor returning None or anything non-dict is silently ignored
    rather than crashing the render."""
    (tmpl_dir / "x.html").write_text("hello {{ x }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.context_processor
    def returns_none():
        return None

    @app.get("/")
    async def index():
        return templates.TemplateResponse("x.html", {"x": "world"})

    from veloce.testclient import TestClient

    resp = TestClient(app).get("/")
    assert resp.body == b"hello world"


def test_render_string_also_picks_up_processors(tmpl_dir: Path):
    """The `render_string` path uses the same merge logic."""
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmpl_dir))

    @app.context_processor
    def with_brand():
        return {"brand": "Veloce"}

    @app.get("/")
    async def index():
        # render_string doesn't return an HTMLResponse — wrap manually.
        from veloce import HTMLResponse

        out = templates.render_string("Welcome to {{ brand }}", {})
        return HTMLResponse(out)

    from veloce.testclient import TestClient

    resp = TestClient(app).get("/")
    assert resp.body == b"Welcome to Veloce"
