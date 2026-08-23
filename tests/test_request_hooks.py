"""@app.before_request / @app.after_request hooks."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Blueprint, JSONResponse, Request, Response, TestClient, Veloce
from veloce.helpers import after_this_request


class TestRequestHooks:
    @pytest.mark.asyncio
    async def test_before_request(self):
        app = Veloce(openapi_url=None)
        log = []

        @app.before_request
        async def log_request(request: Request):
            log.append(f"{request.method} {request.path}")
            return None  # Continue to handler

        @app.get("/test")
        async def test(request: Request):
            return {"ok": True}

        await app.handle_request(make_request(path="/test"))
        assert log == ["GET /test"]

    @pytest.mark.asyncio
    async def test_before_request_short_circuit(self):
        app = Veloce(openapi_url=None)

        @app.before_request
        async def block(request: Request):
            if request.path == "/blocked":
                return JSONResponse({"error": "blocked"}, status_code=403)
            return None

        @app.get("/blocked")
        async def blocked(request: Request):
            return {"should_not": "reach"}

        resp = await app.handle_request(make_request(path="/blocked"))
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_after_request(self):
        app = Veloce(openapi_url=None)

        @app.after_request
        async def add_header(request: Request, response: Response):
            response.headers["X-Custom"] = "added"
            response._encoded = None
            return response

        @app.get("/data")
        async def data(request: Request):
            return {"data": True}

        resp = await app.handle_request(make_request(path="/data"))
        assert resp.headers.get("X-Custom") == "added"


# ── An after-request hook is called by its own signature ─────────────


def _app_with(hook, *, blueprint=False):
    app = Veloce(openapi_url=None)
    app.after_request(hook)

    @app.get("/")
    async def index():
        return {"ok": True}

    return app


def test_a_flask_shaped_hook_taking_only_the_response_works():
    """Taking the response alone is a natural way to write the hook.

    Both values were passed unconditionally, so that shape raised `TypeError`
    and surfaced as a 500.
    """

    async def hook(response):
        response.headers["X-Seen"] = "1"
        return response

    with TestClient(_app_with(hook)) as client:
        assert client.get("/").headers["x-seen"] == "1"


def test_a_hook_taking_both_still_works():
    async def hook(request, response):
        response.headers["X-Path"] = request.path
        return response

    with TestClient(_app_with(hook)) as client:
        assert client.get("/").headers["x-path"] == "/"


def test_a_hook_taking_only_the_request_works():
    seen = []

    async def hook(request):
        seen.append(request.path)

    with TestClient(_app_with(hook)) as client:
        client.get("/")
    assert seen == ["/"]


def test_a_hook_taking_nothing_works():
    called = []

    async def hook():
        called.append(True)

    with TestClient(_app_with(hook)) as client:
        client.get("/")
    assert called == [True]


def test_a_hook_taking_kwargs_receives_both():
    seen = {}

    async def hook(**kwargs):
        seen.update(kwargs)

    with TestClient(_app_with(hook)) as client:
        client.get("/")
    assert set(seen) == {"request", "response"}


def test_a_sync_hook_is_adapted_too():
    """Sync hooks are offloaded, so they take the same path."""

    def hook(response):
        response.headers["X-Sync"] = "1"
        return response

    with TestClient(_app_with(hook)) as client:
        assert client.get("/").headers["x-sync"] == "1"


def test_a_returned_response_still_replaces_the_original():
    async def hook(response):
        return JSONResponse({"replaced": True})

    with TestClient(_app_with(hook)) as client:
        assert client.get("/").json() == {"replaced": True}


def test_a_one_shot_callback_is_adapted_too():
    """`after_this_request` shares the invocation path."""
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def index(request: Request):
        @after_this_request
        async def once(response):
            response.headers["X-Once"] = "1"
            return response

        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/").headers["x-once"] == "1"


def test_a_blueprint_hook_is_adapted_too():
    app = Veloce(openapi_url=None)
    bp = Blueprint("bp", url_prefix="/bp")

    @bp.after_request
    async def hook(response):
        response.headers["X-Bp"] = "1"
        return response

    @bp.get("/x")
    async def x():
        return {"ok": True}

    app.register_blueprint(bp)
    with TestClient(app) as client:
        assert client.get("/bp/x").headers["x-bp"] == "1"
