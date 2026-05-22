"""F5 — the async in-memory test client.

`AsyncTestClient` is the async counterpart of `TestClient`: an async
context manager whose request methods are coroutines, awaited on the
test's own running event loop.
"""

from __future__ import annotations

import pytest

from veloce import AsyncTestClient, Request, UploadFile, Veloce
from veloce.http.response import RedirectResponse


def _app() -> Veloce:
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/")
    async def index():
        return {"hello": "world"}

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    @app.post("/echo")
    async def echo(request: Request):
        return {"received": request.json()}

    @app.post("/form")
    async def form(request: Request):
        data = await request.form()
        return {"name": data.get("name")}

    @app.get("/search")
    async def search(q: str = ""):
        return {"q": q}

    @app.get("/old")
    async def old():
        return RedirectResponse("/", status_code=307)

    @app.get("/whoami")
    async def whoami(request: Request):
        return {"seen": request.cookies.get("token")}

    @app.get("/redir-to-auth")
    async def redir_to_auth():
        return RedirectResponse("/auth-check", status_code=307)

    @app.get("/auth-check")
    async def auth_check(request: Request):
        return {"auth": request.headers.get("authorization")}

    @app.get("/loop")
    async def loop():
        return RedirectResponse("/loop", status_code=307)

    @app.post("/upload")
    async def upload(request: Request):
        form = await request.form()
        f = form.get("file")
        return {"name": f.filename if isinstance(f, UploadFile) else None}

    return app


# ── basic requests ────────────────────────────────────────────────────


async def test_get_returns_json():
    async with AsyncTestClient(_app()) as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert resp.json() == {"hello": "world"}


async def test_factory_method_on_app():
    app = _app()
    async with app.async_test_client() as client:
        resp = await client.get("/items/42")
        assert resp.json() == {"id": 42}


async def test_post_json_body():
    async with AsyncTestClient(_app()) as client:
        resp = await client.post("/echo", json={"a": 1})
        assert resp.json() == {"received": {"a": 1}}


async def test_post_form_data():
    async with AsyncTestClient(_app()) as client:
        resp = await client.post("/form", data={"name": "veloce"})
        assert resp.json() == {"name": "veloce"}


async def test_query_params():
    async with AsyncTestClient(_app()) as client:
        resp = await client.get("/search", params={"q": "async"})
        assert resp.json() == {"q": "async"}


async def test_generic_request_dispatcher():
    async with AsyncTestClient(_app()) as client:
        resp = await client.request("POST", "/echo", json={"k": "v"})
        assert resp.json() == {"received": {"k": "v"}}


# ── cookies and redirects ─────────────────────────────────────────────


async def test_cookie_jar_persists_across_requests():
    async with AsyncTestClient(_app()) as client:
        client.cookies["token"] = "abc"
        resp = await client.get("/whoami")
        assert resp.json() == {"seen": "abc"}


async def test_follow_redirects():
    async with AsyncTestClient(_app(), follow_redirects=True) as client:
        resp = await client.get("/old")
        assert resp.status_code == 200
        assert resp.json() == {"hello": "world"}


async def test_redirect_not_followed_by_default():
    async with AsyncTestClient(_app()) as client:
        resp = await client.get("/old")
        assert resp.status_code == 307
        assert resp.headers.get("location") == "/"


async def test_caller_headers_survive_a_followed_redirect():
    """A caller header (Authorization) must reach the redirected request."""
    async with AsyncTestClient(_app(), follow_redirects=True) as client:
        resp = await client.get("/redir-to-auth", headers={"Authorization": "Bearer XYZ"})
        assert resp.status_code == 200
        assert resp.json() == {"auth": "Bearer XYZ"}


async def test_redirect_loop_hits_the_cap():
    async with AsyncTestClient(_app(), follow_redirects=True) as client:
        with pytest.raises(RuntimeError, match="redirects"):
            await client.get("/loop")


# ── files ─────────────────────────────────────────────────────────────


async def test_post_files_multipart():
    async with AsyncTestClient(_app()) as client:
        resp = await client.post("/upload", files={"file": ("doc.txt", b"data")})
        assert resp.json() == {"name": "doc.txt"}


# ── misuse ────────────────────────────────────────────────────────────


async def test_request_without_async_with_raises():
    client = AsyncTestClient(_app())  # never entered
    with pytest.raises(RuntimeError, match="async context manager"):
        await client.get("/")


# ── lifespan ──────────────────────────────────────────────────────────


async def test_startup_and_shutdown_lifecycle_runs():
    app = Veloce(debug=True, openapi_url=None)
    events: list[str] = []

    @app.on_event("startup")
    async def on_start():
        events.append("startup")

    @app.on_event("shutdown")
    async def on_stop():
        events.append("shutdown")

    @app.get("/")
    async def index():
        return {"ok": True}

    async with AsyncTestClient(app) as client:
        assert events == ["startup"]  # ran on __aenter__
        await client.get("/")

    # __aexit__ ran the shutdown lifecycle.
    assert events == ["startup", "shutdown"]
