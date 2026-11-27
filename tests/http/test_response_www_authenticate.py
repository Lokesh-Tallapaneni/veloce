"""Response.www_authenticate + set_basic_auth_challenge — RFC 9110 §11.6.1."""

from __future__ import annotations

import pytest

from veloce import Response


def test_www_authenticate_none_by_default():
    assert Response().www_authenticate is None


def test_www_authenticate_set_and_read():
    resp = Response()
    resp.www_authenticate = 'Bearer realm="api"'
    assert resp.headers["WWW-Authenticate"] == 'Bearer realm="api"'
    assert resp.www_authenticate == 'Bearer realm="api"'


def test_www_authenticate_none_removes_header():
    resp = Response()
    resp.www_authenticate = "Basic"
    resp.www_authenticate = None
    assert "WWW-Authenticate" not in resp.headers


def test_www_authenticate_reads_existing_header():
    resp = Response()
    resp.headers["WWW-Authenticate"] = 'Digest realm="x"'
    assert resp.www_authenticate == 'Digest realm="x"'


def test_set_basic_auth_challenge_default_realm():
    resp = Response()
    out = resp.set_basic_auth_challenge()
    assert out == 'Basic realm="Authentication Required", charset="UTF-8"'
    assert resp.www_authenticate == out


def test_set_basic_auth_challenge_custom_realm():
    resp = Response()
    resp.set_basic_auth_challenge(realm="Admin Area")
    assert 'realm="Admin Area"' in resp.www_authenticate
    assert resp.www_authenticate.startswith("Basic ")


def test_set_basic_auth_challenge_returns_header_value():
    resp = Response()
    returned = resp.set_basic_auth_challenge("R")
    assert returned == resp.headers["WWW-Authenticate"]


def test_set_basic_auth_challenge_rejects_crlf_in_realm():
    resp = Response()
    with pytest.raises(ValueError, match="illegal control character"):
        resp.set_basic_auth_challenge(realm="ok\r\ninjected: x")


def test_set_basic_auth_challenge_rejects_lone_lf_in_realm():
    resp = Response()
    with pytest.raises(ValueError, match="illegal control character"):
        resp.set_basic_auth_challenge(realm="ok\ninjected: x")


def test_set_basic_auth_challenge_rejects_nul_in_realm():
    resp = Response()
    with pytest.raises(ValueError, match="illegal control character"):
        resp.set_basic_auth_challenge(realm="ok\x00injected")


def test_www_authenticate_setter_rejects_crlf():
    resp = Response()
    with pytest.raises(ValueError, match="illegal control character"):
        resp.www_authenticate = "Bearer\r\nInjected: 1"


def test_www_authenticate_setter_rejects_lone_lf():
    resp = Response()
    with pytest.raises(ValueError, match="illegal control character"):
        resp.www_authenticate = 'Bearer realm="api"\nInjected: 1'


def test_www_authenticate_setter_rejects_lone_cr():
    resp = Response()
    with pytest.raises(ValueError, match="illegal control character"):
        resp.www_authenticate = 'Bearer realm="api"\rInjected: 1'


def test_www_authenticate_setter_does_not_partially_apply_on_crlf():
    resp = Response()
    resp.www_authenticate = "Basic"
    with pytest.raises(ValueError):
        resp.www_authenticate = "Bearer\r\nX-Evil: 1"
    # The pre-existing value must not be replaced by a rejected one.
    assert resp.www_authenticate == "Basic"
