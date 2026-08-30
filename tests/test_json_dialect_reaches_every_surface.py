"""The bare-mapping SSE branch, and the edges of the one-dialect rule.

`JSON_SORT_KEYS` and a custom `json_provider_class` are app-wide settings, and
`dumps_for`'s docstring says so: "Every surface that sends one - a response
body, a websocket frame, a server-sent event - goes through here, so an
application's dialect cannot reach some of them and miss others."

Two did not. `JSONResponse.__init__` and the bare-Mapping branch of
`EventSourceResponse` each called `orjson.dumps` directly, so the same
application emitted two dialects - and `sse.py` did it in the same file where
`ServerSentEvent.json` was doing it correctly.

The *reach* of the rule across handler return shapes is swept by `_ROUTES` in
`test_json_serialiser_consistency.py`, which parametrizes all seven of them
including `/response`. What stays here is the bare-mapping SSE branch, which
that table cannot express, and the edges of the rule: the `from_bytes` escape
hatch, a provider that raises, a response built with no app to consult, and the
lower-layer funnel.
"""

from __future__ import annotations

import pytest

from veloce import JSONResponse, Veloce
from veloce._internal import dumps_current, dumps_for
from veloce.json_provider import DefaultJSONProvider, resolve_dumps
from veloce.sse import EventSourceResponse
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


# ── the bare-mapping SSE branch honours JSON_SORT_KEYS ───────────────


def test_a_bare_mapping_yielded_to_sse_sorts():
    """The defect: the same file encoded `ServerSentEvent.json` correctly."""
    app = _sorted_app()

    @app.get("/s")
    async def s():
        async def gen():
            yield dict(UNSORTED)

        return EventSourceResponse(gen())

    assert TestClient(app).get("/s").text.strip() == 'data: {"a":2,"b":1}'


# ── a custom provider reaches the bare-mapping branch ──────────────


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


def test_from_bytes_bypasses_the_configured_dialect():
    """`from_bytes` is the escape hatch: pre-encoded bytes go out as given.

    The no-dialect case, with the stronger assertions, is
    `test_json_response_classes.py::test_from_bytes_does_not_re_encode`. This
    is the half that belongs in a module about one app having one dialect:
    every *other* surface here is asserted to pick the dialect up, so the one
    that must not has to be stated too, or "reaches every surface" would read
    as covering this one.
    """
    app = Veloce(openapi_url=None)
    app.config["JSON_SORT_KEYS"] = True
    app.json_provider_class = ShoutingProvider
    app.json = ShoutingProvider(app)

    @app.get("/x")
    async def x():
        return JSONResponse.from_bytes(b'{"b":1,"a":2}')

    body = TestClient(app).get("/x").body
    assert body == b'{"b":1,"a":2}'
    assert b"dialect" not in body


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
