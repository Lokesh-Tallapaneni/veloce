"""Tests for `veloce.security._utils` extraction helpers."""

from __future__ import annotations

import pytest

from veloce import Request
from veloce.exceptions import HTTPException
from veloce.security._utils import _extract_api_key, _extract_bearer_token


def _req(authz: str | None = None) -> Request:
    headers = {"authorization": authz} if authz else {}
    return Request(method="GET", path="/", query_string="", headers=headers, body=b"")


# ── _extract_bearer_token ─────────────────────────────────────────────


def test_bearer_token_strips_trailing_whitespace():
    token = _extract_bearer_token(_req("Bearer abc123   "))
    assert token == "abc123"


def test_bearer_token_strips_extra_whitespace_after_scheme():
    # RFC 6750 allows multiple SP/HTAB between scheme and token.
    token = _extract_bearer_token(_req("Bearer   abc123"))
    assert token == "abc123"


def test_bearer_token_strips_both_sides():
    token = _extract_bearer_token(_req("Bearer \t abc123 \t"))
    assert token == "abc123"


def test_bearer_only_whitespace_raises():
    with pytest.raises(HTTPException) as exc:
        _extract_bearer_token(_req("Bearer    "))
    assert exc.value.status_code == 401


def test_bearer_only_whitespace_returns_none_when_not_auto_error():
    assert _extract_bearer_token(_req("Bearer   "), auto_error=False) is None


def test_bearer_token_does_not_strip_nbsp_or_newline():
    # RFC 6750 section 2.1 + RFC 7235 only allow SP/HTAB between scheme and token.
    # Other Unicode whitespace must be retained as part of the token (and almost
    # certainly cause the auth backend to reject the token, which is the point).
    nbsp = " "
    token = _extract_bearer_token(_req(f"Bearer abc{nbsp}"))
    assert token == f"abc{nbsp}"

    token = _extract_bearer_token(_req("Bearer abc\n"))
    assert token == "abc\n"


# ── _extract_api_key ──────────────────────────────────────────────────


def test_api_key_empty_string_raises():
    with pytest.raises(HTTPException) as exc:
        _extract_api_key({"X-API-Key": ""}, "X-API-Key")
    assert exc.value.status_code == 401


def test_api_key_empty_string_returns_none_when_not_auto_error():
    assert _extract_api_key({"X-API-Key": ""}, "X-API-Key", auto_error=False) is None


def test_api_key_missing_still_raises():
    with pytest.raises(HTTPException):
        _extract_api_key({}, "X-API-Key")


def test_api_key_present_returns_value():
    assert _extract_api_key({"X-API-Key": "secret"}, "X-API-Key") == "secret"


def test_api_key_whitespace_only_rejected():
    with pytest.raises(HTTPException) as exc:
        _extract_api_key({"X-API-Key": "   "}, "X-API-Key")
    assert exc.value.status_code == 401


def test_api_key_whitespace_only_returns_none_when_not_auto_error():
    assert _extract_api_key({"X-API-Key": "   "}, "X-API-Key", auto_error=False) is None
