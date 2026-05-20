"""app.jinja_env shared Jinja Environment (TP4)."""

from __future__ import annotations

import pytest

from veloce import Veloce


def test_jinja_env_raises_without_templating():
    app = Veloce(openapi_url=None)
    with pytest.raises(RuntimeError, match="no Jinja environment"):
        _ = app.jinja_env


def test_jinja_env_available_with_template_folder(tmp_path):
    (tmp_path / "x.html").write_text("hi")
    app = Veloce(openapi_url=None, template_folder=str(tmp_path))
    env = app.jinja_env
    assert env is not None
    # It's a real Jinja2 Environment.
    from jinja2 import Environment

    assert isinstance(env, Environment)


def test_jinja_env_is_the_templates_env(tmp_path):
    (tmp_path / "x.html").write_text("hi")
    app = Veloce(openapi_url=None, template_folder=str(tmp_path))
    assert app.jinja_env is app._templates.env


def test_jinja_env_filters_are_mutable(tmp_path):
    (tmp_path / "x.html").write_text("hi")
    app = Veloce(openapi_url=None, template_folder=str(tmp_path))
    app.jinja_env.filters["shout"] = lambda s: s.upper()
    assert "shout" in app.jinja_env.filters


def test_jinja_env_globals_are_mutable(tmp_path):
    (tmp_path / "x.html").write_text("hi")
    app = Veloce(openapi_url=None, template_folder=str(tmp_path))
    app.jinja_env.globals["VERSION"] = "1.0"
    assert app.jinja_env.globals["VERSION"] == "1.0"
