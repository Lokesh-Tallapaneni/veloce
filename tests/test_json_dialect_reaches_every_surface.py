"""One application, one JSON dialect, on every surface that sends JSON.

`JSON_SORT_KEYS` and a custom `json_provider_class` are app-wide settings, and
`dumps_for`'s docstring says so: "Every surface that sends one - a response
body, a websocket frame, a server-sent event - goes through here, so an
application's dialect cannot reach some of them and miss others."

Two did not. `JSONResponse.__init__` and the bare-Mapping branch of
`EventSourceResponse` each called `orjson.dumps` directly, so the same
application emitted two dialects - and `sse.py` did it in the same file where
`ServerSentEvent.json` was doing it correctly.
"""

from __future__ import annotations

import pytest

from veloce import JSONResponse, Veloce
from veloce._internal import dumps_current, dumps_for
from veloce.helpers import jsonify
from veloce.json_provider import DefaultJSONProvider, resolve_dumps
from veloce.sse import EventSourceResponse, ServerSentEvent
from veloce.testclient import TestClient

UNSORTED = {"b": 1, "a": 2}


class ShoutingProvider(DefaultJSONProvider):
    """A provider that stamps every payload, so a bypass is unmistakable."""

    def dumps(self, obj, **kwargs):
        if isinstance(obj, dict):
            obj = {"dialect": "custom", **obj}
        return super().dumps(obj, **kwargs)


def _sorted_app() -> Veloce:
    app = Veloce(openapi_url=None)
    app.config["JSON_SORT_KEYS"] = True
    return app


# ── every surface honours JSON_SORT_KEYS ─────────────────────────────


def test_a_handler_returning_a_dict_sorts():
    app = _sorted_app()

    @app.get("/x")
    async def x():
        return dict(UNSORTED)

    assert TestClient(app).get("/x").text == '{"a":2,"b":1}'


def test_jsonify_sorts():
    app = _sorted_app()

    @app.get("/x")
    async def x():
        return jsonify(dict(UNSORTED))

    assert TestClient(app).get("/x").text == '{"a":2,"b":1}'


def test_a_returned_JSONResponse_sorts():
    """The defect: this one emitted the other dialect."""
    app = _sorted_app()

    @app.get("/x")
    async def x():
        return JSONResponse(dict(UNSORTED))

    assert TestClient(app).get("/x").text == '{"a":2,"b":1}'


def test_a_bare_mapping_yielded_to_sse_sorts():
    """The defect: the same file encoded `ServerSentEvent.json` correctly."""
    app = _sorted_app()

    @app.get("/s")
    async def s():
        async def gen():
            yield dict(UNSORTED)

        return EventSourceResponse(gen())

    assert TestClient(app).get("/s").text.strip() == 'data: {"a":2,"b":1}'


def test_an_explicit_server_sent_event_sorts():
    """The surface that was already correct must stay correct."""
    app = _sorted_app()

    @app.get("/s")
    async def s():
        async def gen():
            yield ServerSentEvent.json(dict(UNSORTED))

        return EventSourceResponse(gen())

    assert 'data: {"a":2,"b":1}' in TestClient(app).get("/s").text


def test_every_surface_emits_the_same_bytes():
    """The property, stated directly: one app, one dialect."""
    app = _sorted_app()

    @app.get("/dict")
    async def as_dict():
        return dict(UNSORTED)

    @app.get("/response")
    async def as_response():
        return JSONResponse(dict(UNSORTED))

    @app.get("/jsonify")
    async def as_jsonify():
        return jsonify(dict(UNSORTED))

    client = TestClient(app)
    bodies = {client.get(p).text for p in ("/dict", "/response", "/jsonify")}
    assert len(bodies) == 1, bodies


# ── a custom provider reaches the same surfaces ──────────────────────


@pytest.mark.parametrize("path", ["/dict", "/response", "/jsonify"])
def test_a_custom_provider_reaches_every_surface(path):
    app = Veloce(openapi_url=None)
    app.json_provider_class = ShoutingProvider
    app.json = ShoutingProvider(app)

    @app.get("/dict")
    async def as_dict():
        return {"n": 1}

    @app.get("/response")
    async def as_response():
        return JSONResponse({"n": 1})

    @app.get("/jsonify")
    async def as_jsonify():
        return jsonify({"n": 1})

    assert "dialect" in TestClient(app).get(path).text


def test_a_custom_provider_reaches_a_bare_sse_mapping():
    app = Veloce(openapi_url=None)
    app.json = ShoutingProvider(app)

    @app.get("/s")
    async def s():
        async def gen():
            yield {"n": 1}

        return EventSourceResponse(gen())

    assert "dialect" in TestClient(app).get("/s").text


# ── outside a request, and other edges ───────────────────────────────


def test_constructing_a_json_response_outside_a_request_still_works():
    """There is no app to ask, so the direct encoder applies."""
    assert JSONResponse(dict(UNSORTED)).body == b'{"b":1,"a":2}'


def test_an_unknown_object_is_stringified_not_rejected():
    """Unchanged by the move: `orjson_default` stringifies rather than raising,
    so the `ValueError` path is reached only by an encoder that refuses."""
    assert JSONResponse({"bad": object()}).body.startswith(b'{"bad":"<object object at')


def test_a_provider_that_refuses_still_surfaces_a_clear_value_error():
    """The `ValueError` contract, exercised through a provider that does raise."""
    app = Veloce(openapi_url=None)

    class RefusingProvider(DefaultJSONProvider):
        def dumps(self, obj, **kwargs):
            raise TypeError("nope")

    app.json = RefusingProvider(app)

    @app.get("/x")
    async def x():
        return JSONResponse({"n": 1})

    with pytest.raises(ValueError, match="not JSON-serializable"):
        TestClient(app).get("/x")


def test_only_a_mapping_becomes_a_json_data_field():
    """Unchanged: a `str` is emitted verbatim, other scalars take `str(item)`."""
    app = _sorted_app()

    @app.get("/s")
    async def s():
        async def gen():
            yield "raw-passthrough"
            yield 42

        return EventSourceResponse(gen())

    body = TestClient(app).get("/s").text
    assert body.startswith("raw-passthrough")
    assert "data: 42" in body


def test_from_bytes_does_not_re_encode():
    """The provider's own response path must not double-encode."""
    response = JSONResponse.from_bytes(b'{"already":"encoded"}')
    assert response.body == b'{"already":"encoded"}'


def test_an_app_with_no_dialect_configured_is_byte_identical():
    """The default path must not change shape for anyone."""
    app = Veloce(openapi_url=None)

    @app.get("/dict")
    async def as_dict():
        return dict(UNSORTED)

    @app.get("/response")
    async def as_response():
        return JSONResponse(dict(UNSORTED))

    client = TestClient(app)
    assert client.get("/dict").text == '{"b":1,"a":2}'
    assert client.get("/response").text == '{"b":1,"a":2}'


def test_the_encode_funnel_is_reachable_from_the_lower_layer():
    """It moved to `_internal` so `http.response` can call it without a cycle."""

    assert dumps_for(None, {"a": 1}) == b'{"a":1}'
    assert dumps_current({"a": 1}) == b'{"a":1}'
    assert resolve_dumps(Veloce(openapi_url=None)) is None
