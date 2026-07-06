"""Enum-typed path parameters."""

import enum

import pytest

from tests.conftest import make_request
from veloce import Veloce


class TestEnumParams:
    @pytest.mark.asyncio
    async def test_enum_path_param(self):
        class Color(str, enum.Enum):
            RED = "red"
            GREEN = "green"
            BLUE = "blue"

        app = Veloce(openapi_url=None)

        @app.get("/color/{color}")
        async def get_color(color: Color):
            return {"color": color.value}

        resp = await app.handle_request(make_request(path="/color/red"))
        import orjson

        assert orjson.loads(resp.body)["color"] == "red"

    @pytest.mark.asyncio
    async def test_enum_invalid(self):
        class Status(str, enum.Enum):
            ACTIVE = "active"
            INACTIVE = "inactive"

        app = Veloce(openapi_url=None)

        @app.get("/status/{status}")
        async def get_status(status: Status):
            return {"status": status.value}

        resp = await app.handle_request(make_request(path="/status/unknown"))
        assert resp.status_code == 422
