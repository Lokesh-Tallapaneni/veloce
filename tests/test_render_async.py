"""Jinja2Templates.render_async — async template rendering."""

from __future__ import annotations

from veloce import HTMLResponse, Veloce
from veloce.contrib.templating import Jinja2Templates
from veloce.testclient import TestClient


async def test_render_async_basic(tmp_path):
    (tmp_path / "hello.html").write_text("Hello {{ name }}!")
    tpl = Jinja2Templates(directory=str(tmp_path))
    out = await tpl.render_async("hello.html", {"name": "async"})
    assert out == "Hello async!"


async def test_render_async_with_loop_construct(tmp_path):
    (tmp_path / "list.html").write_text("{% for n in nums %}{{ n }}{% endfor %}")
    tpl = Jinja2Templates(directory=str(tmp_path))
    out = await tpl.render_async("list.html", {"nums": [1, 2, 3]})
    assert out == "123"


async def test_render_async_empty_context(tmp_path):
    (tmp_path / "static.html").write_text("no vars here")
    tpl = Jinja2Templates(directory=str(tmp_path))
    out = await tpl.render_async("static.html")
    assert out == "no vars here"


async def test_render_async_autoescapes_html(tmp_path):
    (tmp_path / "esc.html").write_text("{{ value }}")
    tpl = Jinja2Templates(directory=str(tmp_path))
    out = await tpl.render_async("esc.html", {"value": "<script>"})
    assert "&lt;script&gt;" in out


async def test_render_async_reuses_async_env(tmp_path):
    (tmp_path / "x.html").write_text("x")
    tpl = Jinja2Templates(directory=str(tmp_path))
    await tpl.render_async("x.html")
    first_env = tpl._async_env
    await tpl.render_async("x.html")
    # The async environment is built once and reused.
    assert tpl._async_env is first_env


async def test_render_async_matches_sync_render(tmp_path):
    (tmp_path / "t.html").write_text("{{ a }}-{{ b }}")
    tpl = Jinja2Templates(directory=str(tmp_path))
    ctx = {"a": "1", "b": "2"}
    assert await tpl.render_async("t.html", ctx) == tpl.render("t.html", ctx)


def test_async_context_processor_contributes_to_render_async(tmp_path):
    """An `async def` context processor must contribute values on the async render path."""

    (tmp_path / "hello.html").write_text("Hello, {{ name }} ({{ flavor }})!")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))

    @app.context_processor
    async def add_flavor():
        return {"flavor": "async-vanilla"}

    @app.get("/")
    async def index():
        return HTMLResponse(await templates.render_async("hello.html", {"name": "alice"}))

    resp = TestClient(app).get("/")
    assert resp.status_code == 200
    assert b"async-vanilla" in resp.body
