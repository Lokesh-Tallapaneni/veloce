"""Subdomain routing."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Veloce
from veloce.testclient import TestClient


def _req(path: str, host: str) -> Request:
    return make_request(
        method="GET",
        path=path,
        query_string="",
        headers={"host": host},
        body=b"",
    )


async def test_subdomain_match_by_leftmost_label_without_server_name():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x", subdomain="api")
    async def api_only():
        return {"hit": "api"}

    # No SERVER_NAME → leftmost-label match.
    r = await app.handle_request(_req("/x", host="api.example.com"))
    assert r.status_code == 200
    import orjson

    assert orjson.loads(r.body) == {"hit": "api"}


async def test_subdomain_mismatch_returns_404():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x", subdomain="api")
    async def api_only():
        return {}

    r = await app.handle_request(_req("/x", host="other.example.com"))
    assert r.status_code == 404


async def test_subdomain_match_with_server_name_configured():
    app = Veloce(debug=True, openapi_url=None)
    app.config["SERVER_NAME"] = "example.com"

    @app.get("/x", subdomain="api")
    async def api_only():
        return {"v": 1}

    r = await app.handle_request(_req("/x", host="api.example.com"))
    assert r.status_code == 200


async def test_subdomain_wildcard_matches_any_subdomain():
    app = Veloce(debug=True, openapi_url=None)

    @app.get("/x", subdomain="*")
    async def any_sub():
        return {"ok": True}

    r1 = await app.handle_request(_req("/x", host="alpha.example.com"))
    r2 = await app.handle_request(_req("/x", host="beta.example.com"))
    assert r1.status_code == 200
    assert r2.status_code == 200


async def test_subdomain_apex_does_not_match_wildcard():
    app = Veloce(debug=True, openapi_url=None)
    app.config["SERVER_NAME"] = "example.com"

    @app.get("/x", subdomain="*")
    async def sub_only():
        return {}

    # Apex (no subdomain) does not match `*`.
    r = await app.handle_request(_req("/x", host="example.com"))
    assert r.status_code == 404


def test_request_subdomain_property_with_server_name():
    app = Veloce(openapi_url=None)
    app.config["SERVER_NAME"] = "example.com"
    req = _req("/x", "api.example.com")
    req.app = app
    assert req.subdomain == "api"
    req2 = _req("/x", "example.com")
    req2.app = app
    assert req2.subdomain == ""


def test_request_subdomain_property_without_server_name():
    """Without SERVER_NAME, leftmost-label heuristic; apex returns ''."""
    app = Veloce(openapi_url=None)
    req_sub = _req("/x", "api.example.com")
    req_sub.app = app
    assert req_sub.subdomain == "api"
    req_apex = _req("/x", "localhost")
    req_apex.app = app
    assert req_apex.subdomain == ""


# ── `subdomain` reads the host through the framework's one reader ────
#
# `_extract_host` is the single host-from-Host-header reader, and the only one
# that understands an IPv6 literal - a bare `2001:db8::1` has no port, and a
# bracketed one hides its colons behind `[]`. `Request.subdomain` hand-rolled
# `split(":", 1)[0]` instead, which is the exact shape the repository's own IPv6
# guardrail forbids, and made it the seventh site answering "what is the host".
#
# An IP literal has no subdomain either way: its dots and colons are address
# structure, not name labels.


def _subdomain(host: str, server_name: str | None = None) -> str:
    from veloce.http.request import Request

    request = Request("GET", "/", "", {"Host": host}, b"")
    if server_name is not None:
        app = Veloce(openapi_url=None)
        app.config["SERVER_NAME"] = server_name
        request.app = app
    return request.subdomain


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("sub.example.com", "sub"),
        ("SUB.EXAMPLE.COM", "sub"),
        ("sub.example.com:8443", "sub"),
        ("example.com", "example"),
    ],
)
def test_a_named_host_still_yields_its_leftmost_label(host, expected):
    assert _subdomain(host) == expected


@pytest.mark.parametrize(
    "host",
    [
        "2001:db8::1",
        "[2001:db8::1]:8080",
        "::ffff:192.0.2.1",
        "[::ffff:192.0.2.1]:443",
        "[a.b.c::1]",
        "192.0.2.1",
        "192.0.2.1:8080",
    ],
)
def test_an_ip_literal_has_no_subdomain(host):
    """The defect: the bracket or an address label leaked out as a subdomain."""
    assert _subdomain(host) == ""


def test_a_configured_server_name_still_strips_the_apex():
    assert _subdomain("tenant.example.com", server_name="example.com") == "tenant"
    assert _subdomain("example.com", server_name="example.com") == ""


# ── one answer to "what subdomain is this" ───────────────────────────
#
# Moved here from `test_extensibility_gaps.py`, a module named for a review
# batch rather than a subject.


def test_an_ip_literal_host_matches_no_subdomain_route():
    """The defect: the router matched `192`, the handler saw `''`."""
    app = Veloce(openapi_url=None)

    @app.get("/", subdomain="192")
    async def h(request):
        return {"subdomain": request.subdomain}

    assert TestClient(app).get("/", headers={"Host": "192.168.1.1"}).status_code == 404


def test_a_named_subdomain_still_matches():
    app = Veloce(openapi_url=None)
    app.config["SERVER_NAME"] = "example.com"

    @app.get("/", subdomain="api")
    async def h(request):
        return {"subdomain": request.subdomain}

    client = TestClient(app)
    assert client.get("/", headers={"Host": "api.example.com"}).json() == {"subdomain": "api"}
    assert client.get("/", headers={"Host": "www.example.com"}).status_code == 404


def test_a_wildcard_subdomain_matches_any_non_empty_one():
    app = Veloce(openapi_url=None)
    app.config["SERVER_NAME"] = "example.com"

    @app.get("/", subdomain="*")
    async def h(request):
        return {"subdomain": request.subdomain}

    client = TestClient(app)
    assert client.get("/", headers={"Host": "api.example.com"}).status_code == 200
    assert client.get("/", headers={"Host": "example.com"}).status_code == 404


def test_the_router_and_the_handler_agree():
    """The property: whatever matched is what the handler is told."""
    app = Veloce(openapi_url=None)
    app.config["SERVER_NAME"] = "example.com"

    @app.get("/", subdomain="api")
    async def h(request):
        return {"subdomain": request.subdomain}

    body = TestClient(app).get("/", headers={"Host": "api.example.com"}).json()
    assert body["subdomain"] == "api"


def test_a_wildcard_does_not_match_an_ip_literal():
    app = Veloce(openapi_url=None)

    @app.get("/", subdomain="*")
    async def h(request):
        return {}

    assert TestClient(app).get("/", headers={"Host": "10.0.0.1"}).status_code == 404
