"""Response.is_json / Response.get_json body inspection."""

from __future__ import annotations

import pytest

from veloce import JSONResponse, Response

# ── is_json ─────────────────────────────────────────────────────────


def test_is_json_true_for_json_response():
    assert JSONResponse({"a": 1}).is_json is True


def test_is_json_true_for_application_json():
    assert Response(content_type="application/json").is_json is True


def test_is_json_true_for_structured_suffix():
    assert Response(content_type="application/vnd.api+json").is_json is True


def test_is_json_false_for_html():
    assert Response(content_type="text/html").is_json is False


def test_is_json_ignores_charset_parameter():
    assert Response(content_type="application/json; charset=utf-8").is_json is True


# ── get_json ────────────────────────────────────────────────────────


def test_get_json_parses_json_response():
    resp = JSONResponse({"name": "alice", "age": 30})
    assert resp.get_json() == {"name": "alice", "age": 30}


def test_get_json_parses_list():
    resp = JSONResponse([1, 2, 3])
    assert resp.get_json() == [1, 2, 3]


def test_get_json_none_for_empty_body():
    assert Response().get_json() is None


def test_get_json_raises_on_invalid_json():
    """`ValueError`, not merely "something" - `pytest.raises(Exception)` here
    would be satisfied by a `TypeError` from a signature change."""
    resp = Response(body=b"not json")
    with pytest.raises(ValueError):
        resp.get_json()
