"""StaticFiles symlink containment — a symlink must not escape the served root."""

from __future__ import annotations

import os

import pytest

from tests.conftest import make_request
from veloce import Request
from veloce.contrib.staticfiles import StaticFiles


def _req(path: str) -> Request:
    return make_request(method="GET", path=path, query_string="", headers={}, body=b"")


async def test_staticfiles_serves_file_inside_root(tmp_path):
    served = tmp_path / "public"
    served.mkdir()
    (served / "ok.txt").write_text("inside")

    sf = StaticFiles(directory=str(served), prefix="/static")
    resp = await sf.handle(_req("/static/ok.txt"))
    assert resp is not None and resp.status_code == 200


async def test_staticfiles_rejects_symlink_escaping_the_root(tmp_path):
    served = tmp_path / "public"
    served.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")

    link = served / "escape.txt"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform / permissions")

    sf = StaticFiles(directory=str(served), prefix="/static")
    resp = await sf.handle(_req("/static/escape.txt"))
    assert resp is not None and resp.status_code == 403
