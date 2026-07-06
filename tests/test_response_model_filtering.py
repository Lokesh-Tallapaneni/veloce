"""response_model_include / response_model_exclude on routes."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce


class TestResponseModelFiltering:
    @pytest.mark.asyncio
    async def test_response_model_exclude_in_openapi(self):
        from pydantic import BaseModel

        class Item(BaseModel):
            name: str
            price: float
            tax: float = 10.0

        app = Veloce(openapi_url=None)

        @app.get(
            "/items/{id}",
            response_model=Item,
            response_model_exclude={"tax"},
        )
        async def get_item(id: int):
            return {"name": "Widget", "price": 9.99, "tax": 1.0}

        from veloce.contrib.openapi import get_openapi_schema

        schema = get_openapi_schema(app)
        assert "/items/{id}" in schema["paths"]

    @pytest.mark.asyncio
    async def test_include_in_schema_false(self):
        app = Veloce(openapi_url=None)

        @app.get("/internal", include_in_schema=False)
        async def internal(request: Request):
            return {"secret": True}

        from veloce.contrib.openapi import get_openapi_schema

        schema = get_openapi_schema(app)
        assert "/internal" not in schema["paths"]

        # But route still works
        resp = await app.handle_request(make_request(path="/internal"))
        assert resp.status_code == 200
