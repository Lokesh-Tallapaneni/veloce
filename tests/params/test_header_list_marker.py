"""Header()-marked list parameters collect every repeated header value."""

from __future__ import annotations

import asyncio

import orjson

from tests._asgi_drive import body_of, drive
from veloce import Header, Veloce
from veloce.testclient import TestClient


def _run_http(app: Veloce, path: str, raw_headers: list[tuple[bytes, bytes]]) -> bytes:
    """Drive one HTTP request through the ASGI surface, returning the body.

    The scope and the message-capturing driver come from `tests/_asgi_drive`;
    this used to be twenty-six lines written out here, verbatim in two modules.
    """
    messages = asyncio.run(drive(app, path=path, headers=raw_headers))
    return body_of(messages)


def test_single_header_value_is_one_item_list():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(x_tag: list[str] = Header(default=[])):
        return {"tags": x_tag}

    with TestClient(app) as client:
        resp = client.get("/x", headers={"x-tag": "solo"})

    assert resp.json() == {"tags": ["solo"]}


def test_missing_optional_header_list_uses_default():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(x_tag: list[str] = Header(default=["fallback"])):
        return {"tags": x_tag}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.json() == {"tags": ["fallback"]}


def test_missing_required_header_list_is_422():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(x_tag: list[str] = Header()):
        return {"tags": x_tag}

    with TestClient(app) as client:
        resp = client.get("/x")

    assert resp.status_code == 422


def test_repeated_headers_collected():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(x_tag: list[str] = Header(default=[])):
        return {"tags": x_tag}

    body = _run_http(app, "/x", [(b"x-tag", b"a"), (b"x-tag", b"b"), (b"x-tag", b"c")])
    assert orjson.loads(body) == {"tags": ["a", "b", "c"]}


def test_repeated_headers_int_coercion():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(x_num: list[int] = Header(default=[])):
        return {"sum": sum(x_num)}

    body = _run_http(app, "/x", [(b"x-num", b"4"), (b"x-num", b"6")])
    assert orjson.loads(body) == {"sum": 10}
