"""The `g` per-request global object."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce, g


class TestGObject:
    async def test_g_per_request(self):
        app = Veloce(openapi_url=None)

        @app.get("/set")
        async def set_g(request: Request):
            g.user = "alice"
            return {"user": g.user}

        @app.get("/get")
        async def get_g(request: Request):
            return {"user": g.get("user", "nobody")}

        # g is reset per request
        resp1 = await app.handle_request(make_request(path="/set"))
        import orjson

        assert orjson.loads(resp1.body)["user"] == "alice"

        resp2 = await app.handle_request(make_request(path="/get"))
        assert orjson.loads(resp2.body)["user"] == "nobody"

    def test_g_attribute_error(self):
        g._reset()
        with pytest.raises(AttributeError):
            _ = g.nonexistent

    def test_g_contains(self):
        g._reset()
        g.test_key = "val"
        assert "test_key" in g
        assert "missing" not in g

    def test_g_setdefault(self):
        g._reset()
        result = g.setdefault("counter", 0)
        assert result == 0
        g.counter = 5
        result = g.setdefault("counter", 0)
        assert result == 5

    def test_g_pop(self):
        g._reset()
        g.temp = "data"
        val = g.pop("temp")
        assert val == "data"
        assert "temp" not in g

    def test_g_delete(self):
        g._reset()
        g.to_delete = "x"
        del g.to_delete
        assert "to_delete" not in g
