"""Request.root_path / Request.script_root — mount-point exposure."""

from __future__ import annotations

import orjson

from tests.conftest import make_request
from veloce import Request, Veloce


def _req(scope: dict | None = None, state: dict | None = None) -> Request:
    r = make_request(
        method="GET",
        path="/x",
        query_string="",
        headers={},
        body=b"",
        scope=scope,
    )
    if state:
        r.state.update(state)
    return r


def test_root_path_empty_by_default():
    assert _req().root_path == ""
    assert _req().script_root == ""


def test_root_path_from_asgi_scope():
    r = _req(scope={"root_path": "/api"})
    assert r.root_path == "/api"
    assert r.script_root == "/api"  # script_root mirrors when no proxy_fix


def test_script_root_proxy_fix_wins_over_scope():
    """Trusted ProxyFix prefix beats the ASGI root_path."""
    r = _req(scope={"root_path": "/internal"}, state={"proxy_fix_prefix": "/public"})
    assert r.script_root == "/public"
    # `root_path` is unchanged — it still reflects what ASGI told us.
    assert r.root_path == "/internal"


def test_root_path_none_scope_safe():
    """Missing/empty scope shouldn't raise."""
    r = make_request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert r.root_path == ""
    assert r.script_root == ""


async def test_mounted_veloce_subapp_sees_mount_prefix_as_root_path():
    """A route inside a mounted Veloce sub-app sees `request.root_path` set to the
    mount prefix, the same as a mounted ASGI app does."""
    sub = Veloce(openapi_url=None)

    @sub.get("/whoami")
    async def whoami(req: Request):
        return {"root_path": req.root_path, "script_root": req.script_root}

    app = Veloce(openapi_url=None)
    app.mount("/gateway", sub)

    resp = await app.handle_request(
        make_request(method="GET", path="/gateway/whoami", query_string="", headers={}, body=b"")
    )
    assert orjson.loads(resp.body) == {"root_path": "/gateway", "script_root": "/gateway"}


async def test_mounted_subapp_slash_redirect_keeps_mount_prefix():
    """A trailing-slash redirect inside a mounted sub-app must point at the
    parent-visible path (with the mount prefix), not the sub-app-local path."""
    sub = Veloce(openapi_url=None)

    @sub.get("/ping/")
    async def ping():
        return {"ok": True}

    app = Veloce(openapi_url=None)
    app.mount("/sub", sub)

    # Request the slash-less variant; the sub-app redirects to the slash form.
    resp = await app.handle_request(
        make_request(method="GET", path="/sub/ping", query_string="", headers={}, body=b"")
    )
    assert resp.status_code == 307
    # Location must carry the mount prefix, not a bare "/ping/".
    assert resp.headers["Location"] == "/sub/ping/"


async def test_top_level_slash_redirect_unaffected():
    """A top-level app (root_path == "") still redirects to a bare path."""
    app = Veloce(openapi_url=None)

    @app.get("/items/")
    async def items():
        return {"ok": True}

    resp = await app.handle_request(
        make_request(method="GET", path="/items", query_string="", headers={}, body=b"")
    )
    assert resp.status_code == 307
    assert resp.headers["Location"] == "/items/"
