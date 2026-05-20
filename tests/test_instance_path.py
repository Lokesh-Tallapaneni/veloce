"""app.instance_path instance folder (C7/CF8)."""

from __future__ import annotations

import os

from veloce import Veloce


def test_instance_path_defaults_under_package_root():
    app = Veloce()
    assert app.instance_path == os.path.join(app.package_root, "instance")


def test_instance_path_explicit_override():
    app = Veloce(instance_path="/srv/myapp/instance")
    assert app.instance_path == "/srv/myapp/instance"


def test_instance_path_is_string():
    assert isinstance(Veloce().instance_path, str)


def test_instance_path_not_auto_created():
    app = Veloce(instance_path="/tmp/veloce-nonexistent-instance-xyz")
    # The property computes a path but does not create the directory.
    assert app.instance_path == "/tmp/veloce-nonexistent-instance-xyz"
    assert not os.path.exists("/tmp/veloce-nonexistent-instance-xyz")


def test_instance_path_ends_with_instance_by_default():
    app = Veloce()
    assert app.instance_path.endswith("instance")
