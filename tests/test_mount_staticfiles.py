"""`app.mount("/static", StaticFiles(...))` must actually serve files,
not silently 500 on every request.

`StaticFiles` is the obvious thing to hand to `mount()` for a static
file tree, but it speaks Veloce's `.handle(request)` protocol, not
the ASGI `__call__(scope, receive, send)` shape `mount()` was wiring
through. The fix routes a `StaticFiles` instance into the
`_static_handlers` list at mount time (the same place
`mount_static()` registers them).
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.contrib.staticfiles import StaticFiles
from veloce.testclient import TestClient


def test_mount_staticfiles_serves_files(tmp_path):
    """`app.mount("/static", StaticFiles(...))` returns the file body."""
    (tmp_path / "hello.txt").write_bytes(b"hello-mount")

    app = Veloce(openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(tmp_path)))

    resp = TestClient(app).get("/static/hello.txt")
    assert resp.status_code == 200
    assert resp.body == b"hello-mount"


def test_mount_staticfiles_respects_mount_prefix(tmp_path):
    """A `StaticFiles` constructed with prefix `"/x"` but mounted at
    `"/y"` should respond at `/y`, not `/x` — the mount call rewrites
    the lookup prefix to match the user's intent.
    """
    (tmp_path / "a.txt").write_bytes(b"alpha")

    app = Veloce(openapi_url=None)
    # Note the mismatch: ctor prefix "/originally" vs mount prefix "/y".
    sf = StaticFiles(directory=str(tmp_path), prefix="/originally")
    app.mount("/y", sf)

    client = TestClient(app)
    served = client.get("/y/a.txt")
    assert served.status_code == 200
    assert served.body == b"alpha"
    # The original ctor prefix is now inert — the handler only owns
    # the mount path.
    miss = client.get("/originally/a.txt")
    assert miss.status_code == 404


def test_mount_non_callable_raises_clear_typeerror():
    """A bare object (not Veloce, not StaticFiles, not callable) used
    to register silently and 500 per request — now it surfaces the
    mistake at mount-time with a message pointing at the right APIs.
    """
    app = Veloce(openapi_url=None)

    class NotAnApp:
        pass

    with pytest.raises(TypeError, match="ASGI application"):
        app.mount("/bad", NotAnApp())


def test_mount_still_accepts_plain_asgi_callable():
    """The fix must not break the legitimate ASGI-app mount path."""
    app = Veloce(openapi_url=None)

    async def asgi_echo(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"asgi-ok"})

    app.mount("/asgi", asgi_echo)
    resp = TestClient(app).get("/asgi/anything")
    assert resp.status_code == 200
    assert resp.body == b"asgi-ok"


# ── must_exist startup validation ────────────────────────────────────


def test_staticfiles_missing_directory_raises(tmp_path):
    missing = str(tmp_path / "does_not_exist")
    with pytest.raises(ValueError, match="does not exist"):
        StaticFiles(directory=missing)


def test_staticfiles_missing_directory_warns_when_opt_out(tmp_path):
    missing = str(tmp_path / "nope")
    with pytest.warns(UserWarning, match="does not exist"):
        sf = StaticFiles(directory=missing, prefix="/static", must_exist=False)
    assert sf.directory.endswith("nope")


def test_staticfiles_existing_directory_ok(tmp_path):
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        StaticFiles(directory=str(tmp_path))  # no raise, no warning


def test_mount_static_missing_directory_raises():
    app = Veloce(openapi_url=None)
    with pytest.raises(ValueError, match="does not exist"):
        app.mount_static(directory="definitely_missing_dir_xyz")


def test_mount_static_missing_directory_opt_out_warns():
    app = Veloce(openapi_url=None)
    with pytest.warns(UserWarning, match="does not exist"):
        app.mount_static(directory="still_missing_dir_xyz", must_exist=False)


def test_staticfiles_checks_search_perm_not_read_by_default(tmp_path, monkeypatch):
    # Serving a known file needs X_OK (search); R_OK (read) is only for listing.
    import os

    from veloce.contrib import staticfiles

    seen = {}

    def _spy(path, mode):
        seen["mode"] = mode
        return True

    monkeypatch.setattr(staticfiles.os, "access", _spy)
    StaticFiles(directory=str(tmp_path))  # directory_index=False
    assert seen["mode"] == os.X_OK


def test_staticfiles_checks_read_perm_when_listing(tmp_path, monkeypatch):
    import os

    from veloce.contrib import staticfiles

    seen = {}
    monkeypatch.setattr(staticfiles.os, "access", lambda p, m: seen.update(mode=m) or True)
    StaticFiles(directory=str(tmp_path), directory_index=True)
    assert seen["mode"] == os.R_OK
