"""`Jinja2Templates.TemplateResponse` media_type / background params."""

from __future__ import annotations

from pathlib import Path

import pytest

from veloce import Request, Veloce
from veloce.background import BackgroundTask, BackgroundTasks
from veloce.contrib.templating import Jinja2Templates
from veloce.http.response import HTMLResponse, Response


def test_template_response_default_is_html(tmp_path: Path):
    (tmp_path / "x.html").write_text("<p>{{ v }}</p>")
    templates = Jinja2Templates(directory=str(tmp_path))
    resp = templates.TemplateResponse("x.html", {"v": "hi"})
    assert isinstance(resp, HTMLResponse)
    assert resp.mimetype == "text/html"
    assert resp.background is None


def test_template_response_media_type_non_html(tmp_path: Path):
    (tmp_path / "feed.xml").write_text("<rss>{{ title }}</rss>")
    templates = Jinja2Templates(directory=str(tmp_path))
    resp = templates.TemplateResponse(
        "feed.xml", {"title": "News"}, media_type="application/rss+xml"
    )
    assert resp.content_type == "application/rss+xml"
    assert resp.body == b"<rss>News</rss>"


async def test_template_response_background_bare_callable(tmp_path: Path):
    (tmp_path / "x.html").write_text("ok")
    templates = Jinja2Templates(directory=str(tmp_path))
    fired: list[str] = []

    def side_effect():
        fired.append("ran")

    resp = templates.TemplateResponse("x.html", {}, background=side_effect)
    assert isinstance(resp.background, BackgroundTask)
    assert resp.background.func is side_effect
    await resp.background.run()
    assert fired == ["ran"]


def test_template_response_background_task_object(tmp_path: Path):
    (tmp_path / "x.html").write_text("ok")
    templates = Jinja2Templates(directory=str(tmp_path))
    task = BackgroundTask(lambda a: None, "arg")
    resp = templates.TemplateResponse("x.html", {}, background=task)
    assert resp.background is task


async def test_template_response_background_tasks_collection(tmp_path: Path):
    (tmp_path / "x.html").write_text("ok")
    templates = Jinja2Templates(directory=str(tmp_path))
    fired: list[int] = []
    tasks = BackgroundTasks()
    tasks.add_task(lambda: fired.append(1))
    tasks.add_task(lambda: fired.append(2))
    resp = templates.TemplateResponse("x.html", {}, background=tasks)
    assert resp.background is tasks
    await resp.background.run_all()
    assert fired == [1, 2]


def test_template_response_background_invalid_type(tmp_path: Path):
    (tmp_path / "x.html").write_text("ok")
    templates = Jinja2Templates(directory=str(tmp_path))
    with pytest.raises(TypeError):
        templates.TemplateResponse("x.html", {}, background=123)


def test_template_response_media_type_returns_base_response(tmp_path: Path):
    (tmp_path / "x.html").write_text("plain")
    templates = Jinja2Templates(directory=str(tmp_path))
    resp = templates.TemplateResponse("x.html", {}, media_type="text/plain")
    assert isinstance(resp, Response)
    assert not isinstance(resp, HTMLResponse)


async def test_template_response_integration(tmp_path: Path):
    (tmp_path / "x.txt").write_text("body-{{ v }}")
    app = Veloce(debug=True, openapi_url=None)
    templates = Jinja2Templates(directory=str(tmp_path))
    fired: list[str] = []

    @app.get("/")
    async def index():
        return templates.TemplateResponse(
            "x.txt",
            {"v": "1"},
            media_type="text/plain",
            background=lambda: fired.append("bg"),
        )

    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert resp.status_code == 200
    assert resp.mimetype == "text/plain"
    assert resp.body == b"body-1"
    # The public seam, rather than a 50ms sleep: exact, and it fails saying the
    # task never ran instead of passing because the sleep happened to be long
    # enough.
    assert await app.wait_for_background_tasks(), "the background task never ran"
    assert fired == ["bg"]
