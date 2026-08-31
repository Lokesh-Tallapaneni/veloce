"""Tuple return values from handlers — (body, status[, headers])."""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Request, Veloce


class TestTupleResponse:
    async def test_tuple_body_status(self):
        app = Veloce(openapi_url=None)

        @app.post("/items")
        async def create(request: Request):
            return {"id": 1}, 201

        resp = await app.handle_request(make_request(method="POST", path="/items"))
        assert resp.status_code == 201

    async def test_tuple_body_status_headers(self):
        app = Veloce(openapi_url=None)

        @app.post("/items")
        async def create(request: Request):
            return {"id": 1}, 201, {"X-Custom": "value"}

        resp = await app.handle_request(make_request(method="POST", path="/items"))
        assert resp.status_code == 201
        assert resp.headers.get("X-Custom") == "value"
