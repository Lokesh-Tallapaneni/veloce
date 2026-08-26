"""Veloce.send_static_file + static_folder/static_url_path attrs."""

from __future__ import annotations

import os
import sys
import types

import pytest

from veloce import FileResponse, Veloce


def test_static_folder_defaults_to_static():
    app = Veloce(openapi_url=None)
    assert app.static_folder == "static"
    assert app.static_url_path == "/static"


def test_send_static_file_serves_from_absolute_static_folder(tmp_path):
    (tmp_path / "hello.txt").write_bytes(b"hi")
    app = Veloce(openapi_url=None)
    app.static_folder = str(tmp_path)
    resp = app.send_static_file("hello.txt")
    assert isinstance(resp, FileResponse)
    assert resp.body == b"hi"


def test_send_static_file_resolves_relative_under_package_root(tmp_path, monkeypatch):
    """A relative `static_folder` resolves against `package_root`.

    This previously passed an **absolute** folder and said so in its own comment,
    deferring to "the relative-path codepath below" - which did not exist. So the
    `os.path.isabs` branch in `send_static_file`, the only reason the option is
    called *relative*, was never executed.

    `package_root` is the directory of `import_name`'s module file, so pointing
    `import_name` at a module whose `__file__` lives under `tmp_path` is what
    exercises the real resolution rather than simulating it.
    """
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / "assets").mkdir()
    (package_dir / "assets" / "a.css").write_bytes(b"body{}")

    module = types.ModuleType("fake_app_module")
    module.__file__ = str(package_dir / "app.py")
    monkeypatch.setitem(sys.modules, "fake_app_module", module)

    app = Veloce(import_name="fake_app_module", openapi_url=None)
    assert app.package_root == str(package_dir)

    # Relative - the branch under test.
    app.static_folder = "assets"
    assert not os.path.isabs(app.static_folder)
    assert app.send_static_file("a.css").body == b"body{}"


def test_a_relative_static_folder_does_not_resolve_against_the_cwd(tmp_path, monkeypatch):
    """The distinction the branch exists for.

    A decoy of the same name under the working directory must not be picked up:
    if it were, the test above would pass for the wrong reason on any machine
    where the two happen to coincide.
    """
    package_dir = tmp_path / "pkg"
    (package_dir / "assets").mkdir(parents=True)
    (package_dir / "assets" / "a.css").write_bytes(b"the right one")

    decoy = tmp_path / "cwd"
    (decoy / "assets").mkdir(parents=True)
    (decoy / "assets" / "a.css").write_bytes(b"the decoy")
    monkeypatch.chdir(decoy)

    module = types.ModuleType("fake_app_module2")
    module.__file__ = str(package_dir / "app.py")
    monkeypatch.setitem(sys.modules, "fake_app_module2", module)

    app = Veloce(import_name="fake_app_module2", openapi_url=None)
    app.static_folder = "assets"
    assert app.send_static_file("a.css").body == b"the right one"


async def test_the_async_variant_resolves_relative_the_same_way(tmp_path, monkeypatch):
    """`send_static_file_async` carries its own copy of the same branch."""
    package_dir = tmp_path / "pkg"
    (package_dir / "assets").mkdir(parents=True)
    (package_dir / "assets" / "a.css").write_bytes(b"body{}")

    module = types.ModuleType("fake_app_module3")
    module.__file__ = str(package_dir / "app.py")
    monkeypatch.setitem(sys.modules, "fake_app_module3", module)

    app = Veloce(import_name="fake_app_module3", openapi_url=None)
    app.static_folder = "assets"
    resp = await app.send_static_file_async("a.css")
    assert resp.body == b"body{}"


def test_an_absolute_static_folder_is_used_as_is(tmp_path):
    """The other side of the branch, which is what the old test really covered."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "a.css").write_bytes(b"body{}")
    app = Veloce(openapi_url=None)
    app.static_folder = str(static_dir)
    assert os.path.isabs(app.static_folder)
    assert app.send_static_file("a.css").body == b"body{}"


def test_send_static_file_safe_join_blocks_traversal():
    """Path traversal via `..` raises 403 — safe_join refusal."""
    from veloce.exceptions import Forbidden

    app = Veloce(openapi_url=None)
    app.static_folder = os.path.dirname(__file__)
    with pytest.raises(Forbidden):
        app.send_static_file("../etc/passwd")


def test_send_static_file_missing_file_raises():
    """Non-existent file lands in FileNotFoundError from FileResponse."""
    app = Veloce(openapi_url=None)
    app.static_folder = os.path.dirname(__file__)
    with pytest.raises(FileNotFoundError):
        app.send_static_file("does-not-exist.txt")


async def test_send_static_file_async_serves_from_absolute_static_folder(tmp_path):
    (tmp_path / "hello.txt").write_bytes(b"hi")
    app = Veloce(openapi_url=None)
    app.static_folder = str(tmp_path)
    resp = await app.send_static_file_async("hello.txt")
    assert isinstance(resp, FileResponse)
    assert resp.body == b"hi"


async def test_send_static_file_async_blocks_traversal():
    from veloce.exceptions import Forbidden

    app = Veloce(openapi_url=None)
    app.static_folder = os.path.dirname(__file__)
    with pytest.raises(Forbidden):
        await app.send_static_file_async("../etc/passwd")
