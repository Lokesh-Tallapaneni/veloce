"""ORJSONResponse + UJSONResponse tests (Q34)."""

from __future__ import annotations

import importlib.util

import pytest

from veloce import JSONResponse, ORJSONResponse, UJSONResponse, Veloce
from veloce import ORJSONResponse as _ORJ
from veloce import UJSONResponse as _UJ
from veloce.testclient import TestClient

# ── ORJSONResponse ─────────────────────────────────────────────────────


def test_orjson_response_is_a_jsonresponse():
    """Semantic alias — `JSONResponse` already uses orjson."""
    assert issubclass(ORJSONResponse, JSONResponse)


def test_orjson_response_serialises_dict():
    resp = ORJSONResponse({"a": 1, "b": "x"})
    assert resp.content_type == "application/json"
    assert resp.body == b'{"a":1,"b":"x"}'
    assert resp.status_code == 200


def test_orjson_response_status_code_override():
    resp = ORJSONResponse({"created": True}, status_code=201)
    assert resp.status_code == 201


def test_orjson_response_as_response_class_kwarg():
    """`response_class=ORJSONResponse` routes the handler return through
    the explicit class — useful for documentation clarity."""
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x", response_class=ORJSONResponse)
    async def x():
        return {"ok": True}

    resp = TestClient(app).get("/x")
    assert resp.status_code == 200
    assert resp.body == b'{"ok":true}'


# ── JSONResponse.from_bytes ──────────────────────────────────────────


def test_from_bytes_does_not_re_encode():
    """`from_bytes` writes the body verbatim — no orjson round-trip."""
    body = b'{"a":1}'
    resp = JSONResponse.from_bytes(body)
    # Identity check on bytes — same object reference, no copy.
    assert resp.body is body
    assert resp.content_type == "application/json"
    assert resp.status_code == 200


def test_from_bytes_preserves_byte_form():
    """Pre-encoded body with non-default formatting survives untouched."""
    # Indented + key-sorted body that the default ctor would not produce.
    pretty = b'{\n  "a": 1,\n  "b": 2\n}'
    resp = JSONResponse.from_bytes(pretty, status_code=201)
    assert resp.body == pretty
    assert resp.status_code == 201
    assert resp.content_type == "application/json"


def test_from_bytes_headers_passthrough():
    resp = JSONResponse.from_bytes(b'{"x":1}', headers={"X-Custom": "v"})
    assert resp.headers.get("X-Custom") == "v"


def test_from_bytes_is_jsonresponse_instance():
    resp = JSONResponse.from_bytes(b"{}")
    assert isinstance(resp, JSONResponse)


def test_from_bytes_rejects_str_body():
    """`from_bytes` is bytes-only; a stray str must fail loudly rather
    than be silently encoded into a body with mismatched charset."""
    with pytest.raises(TypeError, match="bytes or bytearray"):
        JSONResponse.from_bytes("invalid str body")  # type: ignore[arg-type]


def test_from_bytes_rejects_none_body():
    with pytest.raises(TypeError, match="bytes or bytearray"):
        JSONResponse.from_bytes(None)  # type: ignore[arg-type]


def test_from_bytes_accepts_bytearray():
    """`bytearray` is normalized to `bytes` — callers can pass an
    incrementally-built buffer without an explicit `bytes()` copy."""
    buf = bytearray(b'{"x":1}')
    resp = JSONResponse.from_bytes(buf)
    assert resp.body == b'{"x":1}'
    assert isinstance(resp.body, bytes)


def test_from_bytes_caller_content_type_wins():
    """When the caller passes `Content-Type` in `headers`, that value
    takes precedence over the default `application/json` — matches
    Response.encode()'s rule that user headers override framework
    defaults. Lets callers send `application/problem+json` etc."""
    resp = JSONResponse.from_bytes(
        b'{"type":"about:blank","title":"Bad Request"}',
        status_code=400,
        headers={"Content-Type": "application/problem+json"},
    )
    raw = resp.encode().decode("latin-1")
    # The caller's Content-Type appears on the wire.
    assert "Content-Type: application/problem+json" in raw
    # The framework default is NOT also emitted.
    assert "Content-Type: application/json\r\n" not in raw
    # The Response object retains application/json as its base
    # content_type attribute; the override lives in `headers`.
    assert resp.headers["Content-Type"] == "application/problem+json"


def test_from_bytes_uses_subclass_default_media_type():
    """A subclass declaring `default_media_type` gets its Content-Type
    on `from_bytes()` — without having to override the classmethod.
    Guards against the prior bug where `from_bytes` hardcoded
    `application/json`, silently downgrading subclasses like a
    `ProblemJSON` (RFC 9457) variant."""

    class ProblemJSON(JSONResponse):
        default_media_type = "application/problem+json"

    resp = ProblemJSON.from_bytes(b"{}")
    assert isinstance(resp, ProblemJSON)
    assert resp.content_type == "application/problem+json"
    raw = resp.encode().decode("latin-1")
    assert "Content-Type: application/problem+json" in raw
    assert "Content-Type: application/json\r\n" not in raw


def test_subclass_default_media_type_applies_to_init():
    """A subclass `default_media_type` also drives the normal `__init__`
    path, so `ProblemJSON({...})` and `ProblemJSON.from_bytes(b"...")`
    agree on Content-Type."""

    class ProblemJSON(JSONResponse):
        default_media_type = "application/problem+json"

    resp = ProblemJSON({"type": "about:blank", "title": "Bad Request"}, status_code=400)
    assert resp.content_type == "application/problem+json"


# ── UJSONResponse ─────────────────────────────────────────────────────


_HAS_UJSON = importlib.util.find_spec("ujson") is not None


@pytest.mark.skipif(not _HAS_UJSON, reason="ujson not installed")
def test_ujson_response_serialises_dict():
    import orjson

    resp = UJSONResponse({"a": 1})
    assert resp.content_type == "application/json"
    # Compare via re-decode; ujson and orjson differ in whitespace.
    assert orjson.loads(resp.body) == {"a": 1}


@pytest.mark.skipif(_HAS_UJSON, reason="ujson installed; this test verifies the error path")
def test_ujson_response_raises_when_ujson_missing():
    with pytest.raises(ImportError, match="ujson"):
        UJSONResponse({"a": 1})


# ── Module exports ────────────────────────────────────────────────────


def test_classes_in_veloce_exports():

    assert _ORJ is ORJSONResponse
    assert _UJ is UJSONResponse
