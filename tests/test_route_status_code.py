"""status_code= in the route decorator."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce


class TestStatusCodeDecorator:
    @pytest.mark.asyncio
    async def test_status_code_201(self):
        app = Veloce(openapi_url=None)

        @app.post("/items", status_code=201)
        async def create(request: Request):
            return {"id": 1}

        resp = await app.handle_request(make_request(method="POST", path="/items"))
        assert resp.status_code == 201
