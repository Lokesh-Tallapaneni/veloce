"""F5 — the async in-memory test client.

`AsyncTestClient` is the async counterpart of `TestClient`: an async
context manager whose request methods are coroutines, awaited on the
test's own running event loop.
"""

from __future__ import annotations

from veloce import AsyncTestClient, Request, Veloce
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
