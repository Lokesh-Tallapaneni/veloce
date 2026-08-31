"""One JSON dialect, whatever the handler returned.

`JSON_SORT_KEYS` and a custom `JSONProvider` used to reach `jsonify(...)` and
nothing else. A handler returning a bare `dict` — the commonest return there is —
went down a path that never passed an `option=` to the encoder, so it could not
see the setting at all. The same app answered `{"b":1,"a":2}` from one route and
`{"a":2,"b":1}` from another, under its own default configuration.

Every user-facing return type now resolves the same serialiser. Framework wire
formats deliberately do not: a cache key sorts so equal mappings hash alike, and
a signed or protocol payload is not the application's to restyle.

The default is off. Sorting costs 24-49% of the serialise and most JSON APIs do
not do it, so it is available rather than assumed.
"""

from __future__ import annotations

import orjson
import pytest
from pydantic import BaseModel

from veloce import JSONProvider, Veloce, jsonify
from veloce._internal import _b64decode
from veloce.cache import _KEY_OPTIONS
from veloce.http.response import JSONResponse
from veloce.signing import Signer
from veloce.sse import EventSourceResponse, ServerSentEvent
from veloce.testclient import TestClient
from veloce.websocket import WebSocket

_DATA = {"b": 1, "a": 2}
_INSERTION = b'{"b":1,"a":2}'
_SORTED = b'{"a":2,"b":1}'


class Row(BaseModel):
    b: int
    a: int


class ProblemJSON(JSONResponse):
    default_media_type = "application/problem+json"


def _app(**config) -> Veloce:
    app = Veloce(openapi_url=None)
    app.config.update(config)

    @app.get("/dict")
    async def as_dict() -> dict:
        return dict(_DATA)

    @app.get("/list")
    async def as_list() -> list:
        return [dict(_DATA)]

    @app.get("/model")
    async def as_model():
        return Row(b=1, a=2)

    @app.get("/jsonify")
    async def as_jsonify():
        return jsonify(dict(_DATA))

    @app.get("/tuple")
    async def as_tuple():
        return dict(_DATA), 201

    @app.get("/subclass", response_class=ProblemJSON)
    async def as_subclass():
        return dict(_DATA)

    @app.get("/response")
    async def as_response():
        return JSONResponse(dict(_DATA))

    return app


#: Every way a handler can hand back a JSON object.
_ROUTES = ["/dict", "/list", "/model", "/jsonify", "/tuple", "/subclass", "/response"]


# ── The default: nothing sorts ───────────────────────────────────────


@pytest.mark.parametrize("route", _ROUTES)
def test_the_default_keeps_insertion_order(route):
    body = TestClient(_app()).get(route).body
    assert b'"b":1' in body
    assert body.index(b'"b"') < body.index(b'"a"')


def test_the_shipped_default_is_off():
    assert Veloce(openapi_url=None).config["JSON_SORT_KEYS"] is False


# ── Enabling it reaches every return type ────────────────────────────


@pytest.mark.parametrize("route", _ROUTES)
def test_enabling_it_sorts_every_return_type(route):
    body = TestClient(_app(JSON_SORT_KEYS=True)).get(route).body
    assert body.index(b'"a"') < body.index(b'"b"'), route


def test_every_return_type_agrees_with_every_other():
    """The property that was broken: one app, one answer."""
    for sort in (False, True):
        client = TestClient(_app(JSON_SORT_KEYS=sort))
        bodies = {r: client.get(r).body for r in ("/dict", "/model", "/jsonify", "/tuple")}
        assert len(set(bodies.values())) == 1, (sort, bodies)


def test_a_response_subclass_keeps_its_media_type():
    """The dialect must not cost a subclass its declared content type."""
    for sort in (False, True):
        response = TestClient(_app(JSON_SORT_KEYS=sort)).get("/subclass")
        assert response.headers["content-type"].startswith("application/problem+json")


def test_a_tuple_return_keeps_its_status():
    response = TestClient(_app(JSON_SORT_KEYS=True)).get("/tuple")
    assert response.status_code == 201


# ── A custom provider reaches every return type too ──────────────────


class UpperKeyProvider(JSONProvider):
    """A dialect nothing else could produce, so its reach is unambiguous."""

    def dumps(self, obj, **kwargs) -> bytes:
        def upper(value):
            if isinstance(value, dict):
                return {k.upper(): upper(v) for k, v in value.items()}
            if isinstance(value, list):
                return [upper(v) for v in value]
            return value

        return orjson.dumps(upper(obj))

    def loads(self, data):
        return orjson.loads(data)


@pytest.mark.parametrize("route", _ROUTES)
def test_a_custom_provider_reaches_every_return_type(route):
    app = _app()
    app.json_provider_class = UpperKeyProvider
    body = TestClient(app).get(route).body
    assert b'"B"' in body, route
    assert b'"b"' not in body, route


def test_a_custom_provider_still_returns_a_json_response():
    """The coerced type is part of the contract, not an implementation detail."""
    app = _app()
    app.json_provider_class = UpperKeyProvider
    response = TestClient(app).get("/dict")
    assert response.headers["content-type"].startswith("application/json")


# ── What the setting must NOT reach ──────────────────────────────────


def test_a_cache_key_sorts_regardless():
    """Equal mappings must hash alike whatever the app renders responses as."""

    assert _KEY_OPTIONS & orjson.OPT_SORT_KEYS


@pytest.mark.parametrize("sort", [False, True])
def test_a_signed_payload_is_not_restyled_by_the_app(sort: bool):
    """A token's payload is the framework's wire format, not the app's.

    Signed under an active app context, so a `Signer` that started resolving
    the app's provider would see the setting and sort the payload here.
    """
    app = _app(JSON_SORT_KEYS=sort)
    signer = Signer("k")
    with app.app_context():
        token = signer.dumps(dict(_DATA))
        assert signer.loads(token) == _DATA
    payload_b64 = token.split(".")[0]
    assert _b64decode(payload_b64) == _INSERTION


def test_a_framework_error_body_is_not_restyled():
    """An error payload is the framework's wire format, not the app's."""
    app = _app(JSON_SORT_KEYS=True)
    body = TestClient(app).get("/nope").body
    # `detail` before `status_code` is the framework's own ordering; sorting
    # would put `detail` first too, so assert the shape rather than the order.
    assert b"detail" in body


# ── The streaming surfaces follow it too ─────────────────────────────
#
# A WebSocket frame and an SSE event carry application data to a client, the
# same as a response body, so the same dialect applies. They reached the
# encoder directly and were the last two surfaces that did not.


def _socket_app(**config) -> Veloce:
    app = Veloce(openapi_url=None)
    app.config.update(config)

    @app.websocket("/ws")
    async def echo(ws):
        await ws.accept()
        await ws.send_json(dict(_DATA))

    @app.get("/sse")
    async def stream():

        async def events():
            yield ServerSentEvent.json(dict(_DATA))

        return EventSourceResponse(events())

    return app


def test_a_websocket_frame_keeps_insertion_order_by_default():
    with TestClient(_socket_app()).websocket_connect("/ws") as ws:
        assert ws.receive_text() == _INSERTION.decode()


def test_a_websocket_frame_follows_the_setting():
    with TestClient(_socket_app(JSON_SORT_KEYS=True)).websocket_connect("/ws") as ws:
        assert ws.receive_text() == _SORTED.decode()


def test_a_websocket_frame_follows_a_custom_provider():
    app = _socket_app()
    app.json_provider_class = UpperKeyProvider
    with TestClient(app).websocket_connect("/ws") as ws:
        assert b'"B"' in ws.receive_text().encode()


def test_an_sse_event_keeps_insertion_order_by_default():
    assert _INSERTION.decode() in TestClient(_socket_app()).get("/sse").text


def test_an_sse_event_follows_the_setting():
    body = TestClient(_socket_app(JSON_SORT_KEYS=True)).get("/sse").text
    assert _SORTED.decode() in body


async def test_a_socket_built_outside_a_request_still_serialises():
    """No app to consult is not an error; it falls back to the default."""

    class _T:
        def write(self, data): ...
        def writelines(self, data): ...
        def is_closing(self):
            return False

    ws = WebSocket(_T(), {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="})
    assert ws.app is None
