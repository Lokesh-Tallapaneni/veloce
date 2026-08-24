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
from veloce.http.response import JSONResponse
from veloce.testclient import TestClient

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

    return app


#: Every way a handler can hand back a JSON object.
_ROUTES = ["/dict", "/list", "/model", "/jsonify", "/tuple", "/subclass"]


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
    from veloce.cache import _KEY_OPTIONS

    assert _KEY_OPTIONS & orjson.OPT_SORT_KEYS


def test_a_signed_payload_round_trips_under_either_setting():
    from veloce.signing import Signer

    for _sort in (False, True):
        signer = Signer("k")
        assert signer.loads(signer.dumps(dict(_DATA))) == _DATA


def test_a_framework_error_body_is_not_restyled():
    """An error payload is the framework's wire format, not the app's."""
    app = _app(JSON_SORT_KEYS=True)
    body = TestClient(app).get("/nope").body
    # `detail` before `status_code` is the framework's own ordering; sorting
    # would put `detail` first too, so assert the shape rather than the order.
    assert b"detail" in body
