"""Response.www_authenticate + set_basic_auth_challenge — RFC 9110 §11.6.1."""

from __future__ import annotations

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
