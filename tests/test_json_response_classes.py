"""ORJSONResponse + UJSONResponse tests (Q34)."""

from __future__ import annotations

import importlib.util

import pytest

from veloce import JSONResponse, ORJSONResponse, UJSONResponse, Veloce
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
    from veloce import ORJSONResponse as _ORJ
    from veloce import UJSONResponse as _UJ

    assert _ORJ is ORJSONResponse
    assert _UJ is UJSONResponse
