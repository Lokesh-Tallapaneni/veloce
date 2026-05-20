"""Request.url_for — ASGI shape reverse URL resolution."""

from __future__ import annotations

import pytest

from veloce import Request, Veloce


def test_url_for_no_app_raises():
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    with pytest.raises(RuntimeError, match="bound app"):
        req.url_for("anything")


@pytest.mark.asyncio
async def test_url_for_resolves_static_route():
    app = Veloce(openapi_url=None)

    @app.get("/dashboard", name="dash")
    async def dash(request: Request):
        return {"url": request.url_for("dash")}

    import orjson

    req = Request(method="GET", path="/dashboard", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert orjson.loads(resp.body)["url"] == "/dashboard"


@pytest.mark.asyncio
async def test_url_for_with_path_params():
    app = Veloce(openapi_url=None)

    @app.get("/items/{item_id:int}", name="item")
    async def item(request: Request, item_id: int):
        return {"url": request.url_for("item", item_id=42)}

    import orjson

    req = Request(method="GET", path="/items/1", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert orjson.loads(resp.body)["url"] == "/items/42"


@pytest.mark.asyncio
async def test_url_for_unknown_route_raises_build_error():
    from veloce import BuildError

    app = Veloce(openapi_url=None)

    @app.get("/x", name="x")
    async def x(request: Request):
        return {}

    req = Request(method="GET", path="/x", query_string="", headers={}, body=b"")
    req.app = app
    with pytest.raises(BuildError):
        req.url_for("does_not_exist")
