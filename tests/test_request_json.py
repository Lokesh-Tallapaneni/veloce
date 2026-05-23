"""Request.get_json(force=, silent=, cache=) accessor."""

from __future__ import annotations

import pytest

from veloce import Request


def _req(body: bytes = b"", content_type: str = "application/json") -> Request:
    return Request(
        method="POST",
        path="/x",
        query_string="",
        headers={"content-type": content_type} if content_type else {},
        body=body,
    )


# ── Happy path ───────────────────────────────────────────────────────


def test_parses_json_body():
    assert _req(b'{"a": 1}').get_json() == {"a": 1}


def test_empty_body_returns_none():
    assert _req(b"").get_json() is None


def test_non_json_content_type_returns_none():
    """Default behaviour: refuse non-JSON content types."""
    assert _req(b'{"a": 1}', content_type="text/plain").get_json() is None


# ── force ────────────────────────────────────────────────────────────


def test_force_skips_content_type_check():
    assert _req(b'{"a": 1}', content_type="text/plain").get_json(force=True) == {"a": 1}


def test_force_still_returns_none_for_empty_body():
    assert _req(b"", content_type="text/plain").get_json(force=True) is None


# ── silent ───────────────────────────────────────────────────────────


def test_default_raises_on_malformed_json():
    import orjson

    with pytest.raises(orjson.JSONDecodeError):
        _req(b"not-json").get_json()


def test_silent_swallows_parse_error():
    assert _req(b"not-json").get_json(silent=True) is None


# ── cache ────────────────────────────────────────────────────────────


def test_cache_default_returns_same_object():
    req = _req(b'{"a": 1}')
    first = req.get_json()
    second = req.get_json()
    assert first is second  # identity, not just equality


def test_cache_false_reparses():
    req = _req(b'{"a": 1}')
    first = req.get_json()
    second = req.get_json(cache=False)
    assert first == second
    # Different object identity proves the re-parse happened.
    assert first is not second
