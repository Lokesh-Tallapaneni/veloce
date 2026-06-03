"""HTTPBasic scheme — WWW-Authenticate realm escaping (RFC 7235)."""

from __future__ import annotations

import pytest

from veloce import HTTPBasic, HTTPException, Request


def _req(headers: dict | None = None) -> Request:
    return Request(method="GET", path="/x", query_string="", headers=headers or {}, body=b"")


def test_missing_auth_emits_quoted_realm_challenge():
    scheme = HTTPBasic(realm='My "App"')
    with pytest.raises(HTTPException) as exc:
        scheme(_req())
    assert exc.value.status_code == 401
    wwwa = exc.value.headers["WWW-Authenticate"]
    assert wwwa.startswith("Basic ")
    assert r'realm="My \"App\""' in wwwa


def test_realm_with_at_sign_emitted_literally():
    scheme = HTTPBasic(realm="testrealm@example.com")
    with pytest.raises(HTTPException) as exc:
        scheme(_req())
    assert 'realm="testrealm@example.com"' in exc.value.headers["WWW-Authenticate"]


def test_realm_with_backslash_escaped_on_invalid_credentials():
    scheme = HTTPBasic(realm="c:\\x")
    # A malformed Authorization header triggers the second challenge path.
    with pytest.raises(HTTPException) as exc:
        scheme(_req({"authorization": "Basic !!!notbase64"}))
    assert r'realm="c:\\x"' in exc.value.headers["WWW-Authenticate"]


def test_realm_with_control_chars_raises_at_construction():
    with pytest.raises(ValueError):
        HTTPBasic(realm="x\nfoo")


def test_empty_realm_emits_no_challenge_header():
    scheme = HTTPBasic()  # default realm ""
    with pytest.raises(HTTPException) as exc:
        scheme(_req())
    assert "WWW-Authenticate" not in exc.value.headers
