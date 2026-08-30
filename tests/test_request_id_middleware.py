"""RequestIDMiddleware — X-Request-ID generation and passthrough."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Request, Veloce
from veloce.middleware import RequestIDMiddleware


async def test_request_id_middleware():
    app = Veloce(openapi_url=None)
    app.add_middleware(RequestIDMiddleware())

    @app.get("/")
    async def index(request: Request):
        return {"request_id": request.state.get("request_id", "")}

    resp = await app.handle_request(make_request())
    assert "X-Request-ID" in resp.headers


async def test_request_id_preserved():
    app = Veloce(openapi_url=None)
    app.add_middleware(RequestIDMiddleware())

    @app.get("/")
    async def index(request: Request):
        return {"id": request.state["request_id"]}

    resp = await app.handle_request(make_request(headers={"x-request-id": "custom-id-123"}))
    assert resp.headers["X-Request-ID"] == "custom-id-123"
