"""Request.url_for — ASGI shape reverse URL resolution."""

from __future__ import annotations

import orjson
import pytest

from tests.conftest import make_request
from veloce import BuildError, Request, TestClient, Veloce
from veloce.middleware.proxy_fix import ProxyFix


def test_url_for_no_app_raises():
    req = make_request(method="GET", path="/", query_string="", headers={}, body=b"")
    with pytest.raises(RuntimeError, match="bound app"):
        req.url_for("anything")


async def test_url_for_resolves_static_route():
    app = Veloce(openapi_url=None)

    @app.get("/dashboard", name="dash")
    async def dash(request: Request):
        return {"url": request.url_for("dash")}

    req = make_request(method="GET", path="/dashboard", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert orjson.loads(resp.body)["url"] == "/dashboard"


async def test_url_for_with_path_params():
    app = Veloce(openapi_url=None)

    @app.get("/items/{item_id:int}", name="item")
    async def item(request: Request, item_id: int):
        return {"url": request.url_for("item", item_id=42)}

    req = make_request(method="GET", path="/items/1", query_string="", headers={}, body=b"")
    resp = await app.handle_request(req)
    assert orjson.loads(resp.body)["url"] == "/items/42"


async def test_url_for_unknown_route_raises_build_error():

    app = Veloce(openapi_url=None)

    @app.get("/x", name="x")
    async def x(request: Request):
        return {}

    req = make_request(method="GET", path="/x", query_string="", headers={}, body=b"")
    req.app = app
    with pytest.raises(BuildError):
        req.url_for("does_not_exist")


# ── `_external` builds from the live request ─────────────────────────


def _proxied_app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.add_middleware(ProxyFix(x_proto=1, x_host=1, x_port=1, x_prefix=1))

    @app.get("/orders/{oid:int}", name="order")
    async def order(oid: int):
        return {}

    @app.get("/link")
    async def link(request: Request):
        return {"url": request.url_for("order", oid=7, _external=True)}

    return app


_PROXY_HEADERS = {
    "X-Forwarded-Proto": "https",
    "X-Forwarded-Host": "example.com",
    "X-Forwarded-Port": "8443",
    "X-Forwarded-Prefix": "/api",
}


def test_an_external_url_carries_the_recovered_origin_and_prefix():
    """It was built from `SERVER_NAME`, ignoring the request entirely.

    Behind a proxy that terminates TLS on another port and mounts the app under
    a prefix, a generated link pointed at the internal origin - so an emailed
    link was dead. `app.url_for` has no request to read; `Request.url_for` does.
    """
    with TestClient(_proxied_app()) as client:
        body = client.get("/link", headers=_PROXY_HEADERS).json()
    assert body["url"] == "https://example.com:8443/api/orders/7"


def test_an_external_url_without_a_proxy_uses_the_request_origin():
    with TestClient(_proxied_app()) as client:
        assert client.get("/link").json()["url"] == "http://testserver/orders/7"


def test_a_relative_url_is_unchanged():
    app = Veloce(openapi_url=None)

    @app.get("/orders/{oid:int}", name="order")
    async def order(oid: int):
        return {}

    @app.get("/link")
    async def link(request: Request):
        return {"url": request.url_for("order", oid=7)}

    with TestClient(app) as client:
        assert client.get("/link").json()["url"] == "/orders/7"


def test_an_explicit_host_and_scheme_still_win():
    app = Veloce(openapi_url=None)
    app.add_middleware(ProxyFix(x_proto=1, x_host=1, x_port=1, x_prefix=1))

    @app.get("/orders/{oid:int}", name="order")
    async def order(oid: int):
        return {}

    @app.get("/link")
    async def link(request: Request):
        return {
            "url": request.url_for(
                "order", oid=7, _external=True, _scheme="http", _host="other.test"
            )
        }

    with TestClient(app) as client:
        body = client.get("/link", headers=_PROXY_HEADERS).json()
    assert body["url"] == "http://other.test/api/orders/7"


def test_a_query_parameter_survives_the_external_build():
    app = Veloce(openapi_url=None)

    @app.get("/orders/{oid:int}", name="order")
    async def order(oid: int):
        return {}

    @app.get("/link")
    async def link(request: Request):
        return {"url": request.url_for("order", oid=7, _external=True, page="2")}

    with TestClient(app) as client:
        assert client.get("/link").json()["url"] == "http://testserver/orders/7?page=2"


def test_the_app_level_url_for_is_unchanged():
    """`app.url_for` has no request, so it still answers from config."""
    app = Veloce(openapi_url=None)
    app.config["SERVER_NAME"] = "config.test"

    @app.get("/orders/{oid:int}", name="order")
    async def order(oid: int):
        return {}

    assert app.url_for("order", oid=7, _external=True) == "http://config.test/orders/7"
