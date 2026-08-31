"""Bodiless statuses (1xx/204/205/304) carry no body and no default content-type.

RFC 9110: 1xx (Sec. 15.2), 204 (15.3.5), 205 (15.3.6), 304 (15.4.5) MUST NOT
include a payload. Veloce strips the body and suppresses the framework-default
content-type on both the ASGI and native (`Response.encode`) emit paths.
1xx/204/205 advertise `Content-Length: 0`; a 304 may advertise the would-be-200
length (RFC 9110 Sec. 8.6), like a HEAD response.
"""

from __future__ import annotations

import pytest

from veloce import Request, Response, Veloce
from veloce.status import status_permits_body
from veloce.testclient import TestClient


def _ct(resp) -> str | None:
    for k, v in resp.raw_headers:
        if k == b"content-type":
            return v.decode()
    return None


# ── predicate ────────────────────────────────────────────────────────


@pytest.mark.parametrize("code", [100, 103, 199, 204, 205, 304])
def test_status_permits_body_false(code):
    assert status_permits_body(code) is False


@pytest.mark.parametrize("code", [200, 201, 206, 300, 400, 500, None])
def test_status_permits_body_true(code):
    assert status_permits_body(code) is True


# ── ASGI emit path ───────────────────────────────────────────────────


@pytest.mark.parametrize("code", [204, 205, 304])
def test_bodiless_suppresses_content_type_and_body(code):
    app = Veloce(openapi_url=None)

    @app.get("/x", status_code=code)
    async def h():
        return {"stripped": True}

    resp = TestClient(app).get("/x")
    assert resp.status_code == code
    assert resp.body == b""
    assert _ct(resp) is None  # no application/json over zero bytes


@pytest.mark.parametrize("code", [204, 205])
def test_204_205_content_length_zero(code):
    app = Veloce(openapi_url=None)

    @app.get("/x", status_code=code)
    async def h():
        return {"stripped": True}

    resp = TestClient(app).get("/x")
    cl = next((v.decode() for k, v in resp.raw_headers if k == b"content-length"), None)
    assert cl == "0"  # no representation


def test_200_keeps_content_type_and_body():
    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def h():
        return {"ok": True}

    resp = TestClient(app).get("/x")
    assert _ct(resp) is not None and "application/json" in _ct(resp)
    assert resp.body == b'{"ok":true}'


def test_handler_set_content_type_survives_on_304():
    app = Veloce(openapi_url=None)

    @app.get("/x", status_code=304)
    async def h():
        return Response(status_code=304, headers={"Content-Type": "text/plain"})

    resp = TestClient(app).get("/x")
    assert _ct(resp) == "text/plain"  # explicit header is not the framework default


# ── native Response.encode path ──────────────────────────────────────


def test_native_encode_bodiless_omits_content_type_and_body():
    head = Response(status_code=204, body=b"ignored", content_type="application/json").encode()
    assert b"Content-Type" not in head
    assert b"Content-Length: 0" in head
    assert head.endswith(b"\r\n\r\n")  # no body after the blank line


def test_native_encode_200_keeps_content_type_and_body():
    enc = Response(status_code=200, body=b"hi", content_type="text/plain").encode()
    assert b"Content-Type: text/plain" in enc
    assert enc.endswith(b"hi")


def test_native_encode_handler_content_type_survives_bodiless():
    enc = Response(status_code=304, headers={"Content-Type": "text/plain"}).encode()
    assert b"Content-Type: text/plain" in enc


def test_native_encode_304_advertises_representation_length():
    enc = Response(status_code=304, body=b"hello").encode()
    assert b"Content-Length: 5" in enc  # would-be-200 length
    assert enc.endswith(b"\r\n\r\n")  # but no body sent


def test_make_conditional_304_advertises_the_representation_length():
    # The downgrade drops the body, so the length has to be recorded first -
    # otherwise the 304 advertises 0 for a representation that is 5 bytes.

    resp = Response(status_code=200, body=b"hello")
    resp.add_etag()
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={"if-none-match": resp.headers["ETag"]},
        body=b"",
    )
    resp.make_conditional(req)
    assert resp.status_code == 304
    assert b"Content-Length: 5" in resp.encode()
    assert resp.encode().endswith(b"\r\n\r\n")
