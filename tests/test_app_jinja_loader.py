"""app.jinja_loader template loader accessor."""

from __future__ import annotations

from veloce import Veloce


def test_jinja_loader_none_without_templating():
    assert Veloce(openapi_url=None).jinja_loader is None


def test_jinja_loader_is_filesystemloader(tmp_path):
    (tmp_path / "x.html").write_text("hi")
    app = Veloce(openapi_url=None, template_folder=str(tmp_path))
    from jinja2 import FileSystemLoader

    assert isinstance(app.jinja_loader, FileSystemLoader)


def test_jinja_loader_is_the_env_loader(tmp_path):
    (tmp_path / "x.html").write_text("hi")
    app = Veloce(openapi_url=None, template_folder=str(tmp_path))
    assert app.jinja_loader is app.jinja_env.loader
