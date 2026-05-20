"""Regression tests for reported bugs (GitHub issues #1-#6)."""

from veloce import (
    Blueprint,
    Depends,
    EventSourceResponse,
    ServerSentEvent,
    Veloce,
    WebSocket,
)

# ── Issue #1: register_blueprint dropped hidden / WebSocket routes ────


def test_blueprint_hidden_route_still_registered():
    """include_in_schema=False must hide a route from OpenAPI, not unregister it."""
    bp = Blueprint("demo", url_prefix="")

    @bp.get("/hidden", include_in_schema=False)
    async def hidden():
        return {"ok": True}

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)

    assert app.test_client().get("/hidden").status_code == 200


def test_blueprint_websocket_route_still_registered():
    """A blueprint's WebSocket route must enter the app's radix tree."""
    bp = Blueprint("ws_demo", url_prefix="")

    @bp.websocket("/bws")
    async def handler(ws):
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

    app = Veloce(openapi_url=None)
    app.register_blueprint(bp)

    with app.test_client().websocket_connect("/bws") as ws:
        assert ws.receive_text() == "hi"


# ── Issue #2: EventSourceResponse yielding ServerSentEvent over ASGI ──


def test_eventsource_accepts_serversentevent_objects():
    """Yielding ServerSentEvent objects must work over the ASGI transport."""
    app = Veloce(openapi_url=None)

    @app.get("/sse")
    async def sse(request):
        async def generate():
            yield ServerSentEvent(data="hello", event="greeting")

        return EventSourceResponse(generate())

    resp = app.test_client().get("/sse")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.content_type
    assert b"data: hello" in resp.body
    assert b"event: greeting" in resp.body


# ── Issue #3: class dependency __init__ type annotations ─────────────


def test_class_dependency_init_annotations_are_coerced():
    """Depends(SomeClass) must coerce __init__ params by their annotations."""
    app = Veloce(openapi_url=None)

    class Pager:
        def __init__(self, page: int = 1):
            self.page = page
            self.page_type = type(page).__name__

    @app.get("/pager")
    async def pager(p: Pager = Depends(Pager)):
        return {"page_value": p.page, "page_type": p.page_type}

    body = app.test_client().get("/pager?page=5").json()
    assert body == {"page_value": 5, "page_type": "int"}


# ── Issue #4: TestClient WebSocket connect path with a query string ──


def test_testclient_websocket_connect_with_query_string():
    """websocket_connect must split `?query` so route matching + params work."""
    app = Veloce(openapi_url=None)

    @app.websocket("/ws")
    async def handler(ws: WebSocket):
        await ws.accept()
        await ws.send_json(dict(ws.query_params))
        await ws.close()

    with app.test_client().websocket_connect("/ws?token=abc") as ws:
        assert ws.receive_json() == {"token": "abc"}


# ── Issue #6: consistent default Content-Type for a bare str ─────────


def test_bare_str_and_make_response_str_agree_on_content_type():
    """A bare `str` return and make_response(str) must share a media type."""
    app = Veloce(openapi_url=None)

    @app.get("/a")
    async def a(request):
        return "hello"

    @app.get("/b")
    async def b(request):
        return app.make_response("hello")

    client = app.test_client()
    assert client.get("/a").content_type == client.get("/b").content_type
