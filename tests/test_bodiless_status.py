"""Bodiless statuses (1xx/204/205/304) carry no body and no default content-type.

RFC 9110: 1xx (Sec. 15.2), 204 (15.3.5), 205 (15.3.6), 304 (15.4.5) MUST NOT
include a payload. Veloce strips the body and suppresses the framework-default
content-type on both the ASGI and native (`Response.encode`) emit paths, while
keeping `Content-Length: 0` (valid and intermediary-safe).
"""

from __future__ import annotations

import pytest

from veloce import Response, Veloce
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
def test_bodiless_suppresses_content_type_keeps_cl_zero(code):
    app = Veloce(openapi_url=None)

    @app.get("/x", status_code=code)
    async def h():
        return {"stripped": True}

    resp = TestClient(app).get("/x")
    assert resp.status_code == code
    assert resp.body == b""
    assert _ct(resp) is None  # no application/json over zero bytes
    cl = next((v.decode() for k, v in resp.raw_headers if k == b"content-length"), None)
    assert cl == "0"


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
