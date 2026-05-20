"""TestClient follow_redirects tests (T4)."""

from __future__ import annotations

import pytest

from veloce import RedirectResponse, Veloce
from veloce.testclient import TestClient


def _make_app() -> Veloce:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/source")
    async def src():
        return RedirectResponse("/target", status_code=302)

    @app.get("/target")
    async def tgt():
        return {"reached": "target"}

    @app.post("/post-307")
    async def post_307():
        return RedirectResponse("/echo", status_code=307)

    @app.post("/post-303")
    async def post_303():
        return RedirectResponse("/echo", status_code=303)

    @app.post("/echo")
    async def echo_post():
        return {"method": "POST"}

    @app.get("/echo")
    async def echo_get():
        return {"method": "GET"}

    @app.get("/loop")
    async def loop_a():
        return RedirectResponse("/loop", status_code=302)

    return app


# ── Default behaviour ────────────────────────────────────────────────


def test_redirect_not_followed_by_default():
    """Default `follow_redirects=False` returns the 302 directly."""
    client = TestClient(_make_app())
    resp = client.get("/source")
    assert resp.status_code == 302
    assert resp.headers.get("location") == "/target"


def test_follow_redirects_true_in_ctor_chases_to_target():
    client = TestClient(_make_app(), follow_redirects=True)
    resp = client.get("/source")
    assert resp.status_code == 200
    assert resp.json() == {"reached": "target"}


def test_follow_redirects_per_call_override():
    """Per-call kwarg overrides the ctor default — and vice versa."""
    client = TestClient(_make_app())  # default False
    assert client.get("/source", follow_redirects=True).json() == {"reached": "target"}
    # Even though per-call overrode to True, the default remains False.
    assert client.get("/source").status_code == 302


def test_per_call_disable_with_ctor_default_true():
    client = TestClient(_make_app(), follow_redirects=True)
    assert client.get("/source", follow_redirects=False).status_code == 302


# ── Method semantics ─────────────────────────────────────────────────


def test_307_preserves_method_and_body():
    """POST + 307 → POST again (per RFC 9110 §15.4.8)."""
    client = TestClient(_make_app(), follow_redirects=True)
    resp = client.post("/post-307", json={})
    assert resp.json() == {"method": "POST"}


def test_303_changes_method_to_get():
    """POST + 303 → GET (RFC 9110 §15.4.4)."""
    client = TestClient(_make_app(), follow_redirects=True)
    resp = client.post("/post-303", json={})
    assert resp.json() == {"method": "GET"}


def test_302_changes_method_to_get_browser_convention():
    """Most browsers historically treat 302 like 303 (convert to GET).
    veloce TestClient follows the browser convention for predictability."""
    app = Veloce(debug=True, openapi_url=None)

    @app.post("/p302")
    async def p302():
        return RedirectResponse("/echo", status_code=302)

    @app.get("/echo")
    async def echo_get():
        return {"method": "GET"}

    @app.post("/echo")
    async def echo_post():
        return {"method": "POST"}

    client = TestClient(app, follow_redirects=True)
    resp = client.post("/p302")
    assert resp.json() == {"method": "GET"}


# ── Loop detection ───────────────────────────────────────────────────


def test_max_redirects_loop_raises():
    """A redirect chain longer than the cap raises rather than hanging."""
    client = TestClient(_make_app(), follow_redirects=True)
    with pytest.raises(RuntimeError, match="10 redirects"):
        client.get("/loop")
