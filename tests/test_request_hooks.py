"""@app.before_request / @app.after_request hooks."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import JSONResponse, Request, Response, Veloce


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
