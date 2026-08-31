"""`instance_path` names a definite directory.

It names "a per-deployment writable directory for config, SQLite files,
uploads" - and a relative value resolves against whatever directory the process
was launched from, so the same deployment would write its database somewhere
different depending on how it was started. The computed default is absolute;
only an explicit override could be relative.
"""

from __future__ import annotations

import os

import pytest

from veloce import Veloce
from veloce.testclient import TestClient


def _app(**kwargs) -> Veloce:
    app = Veloce(**kwargs)

    @app.get("/x")
    async def x() -> dict:
        return {"ok": True}

    return app


# ── instance_path names a definite directory ─────────────────────────


@pytest.mark.parametrize("value", ["var/data", "instance", "./instance", "../shared", "a/b/c", ""])
def test_a_relative_instance_path_is_refused(value):
    """The defect: it resolved against the launch directory."""
    with pytest.raises(ValueError, match="instance_path must be an absolute path"):
        Veloce(openapi_url=None, instance_path=value)


def test_the_refusal_says_why():
    with pytest.raises(ValueError, match="current working directory"):
        Veloce(openapi_url=None, instance_path="var/data")


def test_the_refusal_shows_the_value():
    with pytest.raises(ValueError, match="'var/data'"):
        Veloce(openapi_url=None, instance_path="var/data")


def test_an_absolute_instance_path_is_kept(tmp_path):
    directory = str(tmp_path)
    app = Veloce(openapi_url=None, instance_path=directory)
    assert app.instance_path == directory


@pytest.mark.parametrize(
    "value",
    [
        "/srv/myapp/instance",
        "/var/lib/app",
        r"\\fileserver\share",
    ],
)
def test_a_rooted_path_is_accepted_on_any_platform(value):
    """`os.path.isabs("/srv/app")` is False on Windows - no drive letter.

    Refusing on that would reject a POSIX deployment path written on a Windows
    development machine, which is the ordinary case for this project. What the
    check is actually for is a path relative to the working directory, and a
    leading separator is not that.
    """
    assert Veloce(openapi_url=None, instance_path=value).instance_path == value


def test_the_computed_default_is_absolute():
    """It always was; the override was the only way to get a relative one."""
    assert os.path.isabs(Veloce(openapi_url=None).instance_path)


def test_the_computed_default_sits_beside_the_package():
    app = Veloce(openapi_url=None)
    assert app.instance_path == os.path.join(app.package_root, "instance")


def test_no_instance_path_still_computes_one():
    app = Veloce(openapi_url=None, instance_path=None)
    assert app.instance_path.endswith("instance")


def test_the_directory_is_still_not_created(tmp_path):
    """Documented: the caller decides whether to `mkdir` it."""
    target = str(tmp_path / "not-there")
    app = Veloce(openapi_url=None, instance_path=target)
    assert app.instance_path == target
    assert not os.path.exists(target)


def test_an_absolute_path_that_does_not_exist_is_accepted(tmp_path):
    """Absoluteness is the requirement, not existence.

    The premise is asserted rather than assumed. This used to build a fixed
    name under `tempfile.gettempdir()`, shared by every run on the machine:
    two concurrent runs collide, and anything that left that name behind would
    have made the test pass while no longer testing what it says.
    """
    target = str(tmp_path / "veloce-nonexistent-instance")
    assert not os.path.exists(target)
    app = Veloce(openapi_url=None, instance_path=target)
    assert app.instance_path == target


def test_the_app_still_serves_with_an_instance_path(tmp_path):
    app = _app(openapi_url=None, instance_path=str(tmp_path))
    assert TestClient(app).get("/x").json() == {"ok": True}
