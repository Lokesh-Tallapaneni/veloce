"""app.mount() — mounting a sub-application under a path prefix."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce


class TestMountSubApps:
    @pytest.mark.asyncio
    async def test_mount(self):
        main = Veloce(openapi_url=None)
        sub = Veloce(openapi_url=None)

        @sub.get("/items")
        async def items(request: Request):
            return [{"id": 1}]

        main.mount("/api", sub)

        resp = await main.handle_request(make_request(path="/api/items"))
        assert resp.status_code == 200
        import orjson

        assert orjson.loads(resp.body) == [{"id": 1}]
