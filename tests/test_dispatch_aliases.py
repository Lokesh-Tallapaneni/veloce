"""app.preprocess_request / process_response / make_response."""

from __future__ import annotations

import orjson

from tests.conftest import make_request
from veloce import Request, Response, Veloce
from veloce.testclient import TestClient


def _req() -> Request:
    return make_request(method="GET", path="/", query_string="", headers={}, body=b"")


# ── preprocess_request ───────────────────────────────────────────────


async def test_preprocess_runs_hooks_in_order():
    app = Veloce()
    calls: list[int] = []

    @app.before_request
    async def h1(request):
        calls.append(1)

    @app.before_request
    async def h2(request):
        calls.append(2)

    result = await app.preprocess_request(_req())
    assert result is None
    assert calls == [1, 2]


async def test_preprocess_short_circuits_on_first_non_none():
    app = Veloce()

    @app.before_request
    async def h1(request):
        return "early-return"

    @app.before_request
    async def h2(request):
        raise RuntimeError("should not run")

    result = await app.preprocess_request(_req())
    assert result == "early-return"


# ── process_response ─────────────────────────────────────────────────


async def test_process_response_runs_hooks_reversed():
    app = Veloce()
    order: list[int] = []

    @app.after_request
    async def h1(request, response):
        order.append(1)
        return response

    @app.after_request
    async def h2(request, response):
        order.append(2)
        return response

    resp = Response(body=b"x")
    await app.process_response(_req(), resp)
    # Reversed: last registered runs first.
    assert order == [2, 1]


async def test_process_response_replaces_when_hook_returns_new():
    app = Veloce()

    @app.after_request
    async def replace(request, response):
        return Response(body=b"replaced")

    original = Response(body=b"orig")
    out = await app.process_response(_req(), original)
    assert out.body == b"replaced"


async def test_process_response_keeps_response_when_hook_returns_none():
    app = Veloce()

    @app.after_request
    async def noop(request, response):
        return None

    original = Response(body=b"keep")
    out = await app.process_response(_req(), original)
    assert out is original


# ── make_response ────────────────────────────────────────────────────


def test_make_response_passthrough_for_response():
    app = Veloce()
    resp = Response(body=b"x")
    assert app.make_response(resp) is resp


def test_make_response_str_wraps_html():
    app = Veloce()
    resp = app.make_response("<h1>hi</h1>")
    assert resp.body == b"<h1>hi</h1>"
    assert "text/html" in resp.content_type


def test_make_response_bytes_wraps_html():
    app = Veloce()
    resp = app.make_response(b"raw")
    assert resp.body == b"raw"


def test_make_response_dict_json_encodes():
    app = Veloce()
    resp = app.make_response({"a": 1})
    assert orjson.loads(resp.body) == {"a": 1}


def test_make_response_list_json_encodes():
    app = Veloce()
    resp = app.make_response([1, 2, 3])
    assert orjson.loads(resp.body) == [1, 2, 3]


def test_make_response_tuple_status():
    app = Veloce()
    resp = app.make_response(("body", 201))
    assert resp.status_code == 201
    assert resp.body == b"body"


def test_make_response_tuple_status_and_headers():
    app = Veloce()
    resp = app.make_response(("body", 202, {"X-Foo": "bar"}))
    assert resp.status_code == 202
    assert resp.headers["X-Foo"] == "bar"


def test_make_response_tuple_headers_only():
    app = Veloce()
    resp = app.make_response(("body", {"X-Foo": "bar"}))
    assert resp.headers["X-Foo"] == "bar"


def test_make_response_matches_dispatch_for_an_unserialisable_value():
    """`app.make_response` used to raise where a handler returning the same
    value was answered `200`. It now agrees with dispatch, whose fallback
    stringifies an unknown object through `orjson_default`. Whether that
    stringification is the right default is a separate question - the point
    here is that one framework gives one answer.
    """

    app = Veloce(openapi_url=None)

    @app.get("/o")
    async def unserialisable():
        return object()

    via_dispatch = TestClient(app).get("/o")
    via_helper = app.make_response(object())
    assert via_dispatch.status_code == via_helper.status_code == 200
    assert via_dispatch.text.startswith('"<object object at')
    assert via_helper.body.decode().startswith('"<object object at')


def test_bare_str_return_and_make_response_str_share_content_type():
    """A bare `str` return and make_response(str) produce the same media type."""
    app = Veloce(openapi_url=None)

    @app.get("/a")
    async def a(request):
        return "hello"

    @app.get("/b")
    async def b(request):
        return app.make_response("hello")

    client = app.test_client()
    assert client.get("/a").content_type == client.get("/b").content_type
