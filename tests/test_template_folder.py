"""Veloce(template_folder=...) auto-binds Jinja2Templates."""

from __future__ import annotations

import os
import sys
import types

from veloce import Veloce
from veloce.contrib.templating import Jinja2Templates, render_template


def _bind(app: Veloce):
    from veloce.helpers import _current_app_var

    return _current_app_var.set(app)


def _unbind(token):
    from veloce.helpers import _current_app_var

    _current_app_var.reset(token)


def test_no_template_folder_leaves_templates_none():
    app = Veloce(openapi_url=None)
    assert app.template_folder is None
    assert app._templates is None


def test_absolute_template_folder_binds_jinja2templates(tmp_path):
    (tmp_path / "hello.html").write_text("Hi {{ name }}!")
    app = Veloce(openapi_url=None, template_folder=str(tmp_path))
    assert isinstance(app._templates, Jinja2Templates)

    token = _bind(app)
    try:
        assert render_template("hello.html", name="alice") == "Hi alice!"
    finally:
        _unbind(token)


def test_relative_template_folder_resolves_under_package_root(tmp_path, monkeypatch):
    """A relative folder is anchored to `app.package_root`.

    This previously built an app with an `import_name`, abandoned it, rendered
    through a *second* app configured with an absolute folder, and closed with a
    "sanity check" on the app it never used - so the `os.path.isabs` branch it is
    named for never ran.

    `package_root` is the directory of `import_name`'s module file, so
    registering a module whose `__file__` lives under `tmp_path` exercises the
    real resolution.
    """
    package_dir = tmp_path / "pkg"
    (package_dir / "templates").mkdir(parents=True)
    (package_dir / "templates" / "x.html").write_text("X={{ x }}")

    module = types.ModuleType("template_folder_demo")
    module.__file__ = str(package_dir / "app.py")
    monkeypatch.setitem(sys.modules, "template_folder_demo", module)

    app = Veloce(
        openapi_url=None,
        import_name="template_folder_demo",
        template_folder="templates",
    )
    assert app.package_root == str(package_dir)
    assert not os.path.isabs(app.template_folder)
    assert app._templates is not None

    token = _bind(app)
    try:
        assert render_template("x.html", x=7) == "X=7"
    finally:
        _unbind(token)


def test_a_relative_template_folder_does_not_resolve_against_the_cwd(tmp_path, monkeypatch):
    """A decoy of the same name under the working directory must not win."""
    package_dir = tmp_path / "pkg"
    (package_dir / "templates").mkdir(parents=True)
    (package_dir / "templates" / "x.html").write_text("RIGHT")

    decoy = tmp_path / "cwd"
    (decoy / "templates").mkdir(parents=True)
    (decoy / "templates" / "x.html").write_text("DECOY")
    monkeypatch.chdir(decoy)

    module = types.ModuleType("template_folder_demo2")
    module.__file__ = str(package_dir / "app.py")
    monkeypatch.setitem(sys.modules, "template_folder_demo2", module)

    app = Veloce(
        openapi_url=None,
        import_name="template_folder_demo2",
        template_folder="templates",
    )
    token = _bind(app)
    try:
        assert render_template("x.html") == "RIGHT"
    finally:
        _unbind(token)


def test_an_absolute_template_folder_is_used_as_is(tmp_path):
    """The other side of the branch - what the old test actually covered."""
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "x.html").write_text("X={{ x }}")

    app = Veloce(openapi_url=None, template_folder=str(tmp_path / "templates"))
    assert os.path.isabs(app.template_folder)
    assert app._templates is not None

    token = _bind(app)
    try:
        assert render_template("x.html", x=7) == "X=7"
    finally:
        _unbind(token)
