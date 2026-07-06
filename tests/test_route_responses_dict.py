"""responses={...} dict per route — extra documented status codes."""

from __future__ import annotations

import pytest

from veloce import Veloce


class TestMultipleResponses:
    @pytest.mark.asyncio
    async def test_responses_in_route(self):
        app = Veloce(openapi_url=None)

        @app.get(
            "/items/{id}",
            responses={
                404: {"description": "Item not found"},
                403: {"description": "Not authorized"},
            },
        )
        async def get_item(id: int):
            return {"id": id}

        routes = app._collect_all_routes()
        assert len(routes) == 1
        _, _, info = routes[0]
        assert 404 in info.responses
        assert 403 in info.responses
