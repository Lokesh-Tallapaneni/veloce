"""F6 — `add_middleware` accepts standard ASGI middleware.

A class that is not a veloce `Middleware` subclass is treated as a plain
ASGI middleware: it wraps the whole application as `cls(app, **options)`,
which is what lets the third-party ASGI ecosystem (tracing, profiling,
observability) plug into a veloce app.
"""

from __future__ import annotations

import pytest

from veloce import Veloce
from veloce.middleware import BaseHTTPMiddleware, Middleware


class _HeaderMiddleware:
    """A standard ASGI middleware that appends a response header."""

    def __init__(self, app, name="X-ASGI", value="yes"):
        self.app = app
        self.name = name
        self.value = value

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((self.name.lower().encode(), self.value.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)


# ── basic wrapping ────────────────────────────────────────────────────


def test_asgi_middleware_wraps_the_app():
    app = Veloce(openapi_url=None)
    app.add_middleware(_HeaderMiddleware)

    @app.get("/")
    async def index():
        return {"ok": True}

    resp = app.test_client().get("/")
    assert resp.status_code == 200
    assert resp.headers.get("X-ASGI") == "yes"


def test_asgi_middleware_receives_constructor_options():
    app = Veloce(openapi_url=None)
    app.add_middleware(_HeaderMiddleware, name="X-Trace-Id", value="abc123")

    @app.get("/")
    async def index():
        return {"ok": True}

    resp = app.test_client().get("/")
    assert resp.headers.get("X-Trace-Id") == "abc123"


def test_asgi_middleware_class_not_added_to_native_pipeline():
    app = Veloce(openapi_url=None)
    app.add_middleware(_HeaderMiddleware)
    assert len(app._asgi_middleware) == 1
    assert app.middlewares == ()


# ── ordering ──────────────────────────────────────────────────────────


def test_asgi_middleware_first_added_is_outermost():
    order: list[str] = []

    def make(tag):
        class _MW:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                order.append(f"enter:{tag}")
                await self.app(scope, receive, send)
                order.append(f"exit:{tag}")

        return _MW

    app = Veloce(openapi_url=None)
    app.add_middleware(make("outer"))
    app.add_middleware(make("inner"))

    @app.get("/")
    async def index():
        return {"ok": True}

    app.test_client().get("/")
    assert order == ["enter:outer", "enter:inner", "exit:inner", "exit:outer"]


# ── short-circuit ─────────────────────────────────────────────────────


def test_asgi_middleware_can_short_circuit():
    class _Gate:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope.get("path") == "/blocked":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"text/plain")],
                    }
                )
                await send({"type": "http.response.body", "body": b"blocked"})
                return
            await self.app(scope, receive, send)

    app = Veloce(openapi_url=None)
    app.add_middleware(_Gate)

    @app.get("/blocked")
    async def blocked():
        return {"handler": "ran"}

    @app.get("/ok")
    async def ok():
        return {"handler": "ran"}

    client = app.test_client()
    blocked_resp = client.get("/blocked")
    assert blocked_resp.status_code == 403
    assert b"blocked" in blocked_resp.body
    ok_resp = client.get("/ok")
    assert ok_resp.status_code == 200
    assert b"ran" in ok_resp.body


# ── native middleware is unaffected ───────────────────────────────────


def test_native_middleware_class_still_uses_the_pipeline():
    class _NativeMW(Middleware):
        async def process_response(self, request, response):
            response.headers["X-Native"] = "1"
            return response

    app = Veloce(openapi_url=None)
    app.add_middleware(_NativeMW)

    @app.get("/")
    async def index():
        return {"ok": True}

    # A `Middleware` subclass goes to the native pipeline, not the ASGI stack.
    assert app._asgi_middleware == []
    assert len(app.middlewares) == 1

    resp = app.test_client().get("/")
    assert resp.headers.get("X-Native") == "1"


def test_asgi_and_native_middleware_compose():
    class _NativeMW(Middleware):
        async def process_response(self, request, response):
            response.headers["X-Native"] = "1"
            return response

    app = Veloce(openapi_url=None)
    app.add_middleware(_HeaderMiddleware)
    app.add_middleware(_NativeMW)

    @app.get("/")
    async def index():
        return {"ok": True}

    resp = app.test_client().get("/")
    assert resp.headers.get("X-ASGI") == "yes"
    assert resp.headers.get("X-Native") == "1"


# ── misrouting is rejected with a clear error ─────────────────────────


def test_base_http_middleware_class_rejected():
    """A `BaseHTTPMiddleware` subclass is dispatch-shape, not ASGI — it
    must be rejected with a message pointing at `add_http_middleware`."""

    class _Dispatch(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            return await call_next(request)

    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="add_http_middleware"):
        app.add_middleware(_Dispatch)


def test_non_middleware_instance_rejected():
    """A bare object instance cannot be an ASGI middleware (veloce must
    supply the wrapped app) — it is rejected at registration time."""
    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="ASGI middleware"):
        app.add_middleware(object())


# ── ASGI middleware sees every scope type ─────────────────────────────


def test_asgi_middleware_sees_websocket_scope():
    seen: list[str] = []

    class _ScopeRecorder:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            seen.append(scope["type"])
            await self.app(scope, receive, send)

    app = Veloce(openapi_url=None)
    app.add_middleware(_ScopeRecorder)

    @app.websocket("/ws")
    async def echo(ws):
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

    client = app.test_client()
    with client.websocket_connect("/ws") as conn:
        assert conn.receive_text() == "hi"
    assert "websocket" in seen
