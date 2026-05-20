"""Request.root_path / Request.script_root — mount-point exposure."""

from __future__ import annotations

from veloce import Request


def _req(scope: dict | None = None, state: dict | None = None) -> Request:
    r = Request(
        method="GET",
        path="/x",
        query_string="",
        headers={},
        body=b"",
        scope=scope,
    )
    if state:
        r._state.update(state)
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
    r = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert r.root_path == ""
    assert r.script_root == ""
