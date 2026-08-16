"""Duplicate header suppression on the wire — HTTP names fold case (RFC 9110 Sec. 5.1)."""

from __future__ import annotations

from veloce import JSONResponse, Middleware, Response, Veloce
from veloce.middleware.security import CSPMiddleware


def _csp_lines(raw_headers):
    return [(k, v) for k, v in raw_headers if k.decode().lower() == "content-security-policy"]


def test_native_encode_emits_one_line_per_field():
    # Two spellings of one field are one field: the last one written wins and
    # only it reaches the wire.
    resp = Response(body=b"x", content_type="text/plain")
    resp.headers["Content-Security-Policy"] = "default-src 'self'"
    resp.headers["content-security-policy"] = "default-src *"
    head = resp.encode().split(b"\r\n\r\n", 1)[0]
    lines = [ln for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-security-policy:")]
    assert lines == [b"content-security-policy: default-src *"]


def test_native_encode_keeps_the_written_casing():
    # Folding must not lower-case the emitted name.
    resp = Response(body=b"x", content_type="text/plain")
    resp.headers["X-Mixed-Case"] = "v"
    assert b"X-Mixed-Case: v" in resp.encode()


def test_native_encode_keeps_every_set_cookie():
    # Set-Cookie is legitimately multi-valued and must never be folded away.
    resp = Response(body=b"x", content_type="text/plain")
    resp.set_cookie("a", "1")
    resp.set_cookie("b", "2")
    head = resp.encode().split(b"\r\n\r\n", 1)[0]
    cookies = [ln for ln in head.split(b"\r\n") if ln.lower().startswith(b"set-cookie:")]
    assert len(cookies) == 2


def test_asgi_emits_one_csp_header_when_middleware_overrides():
    # Browsers intersect duplicate CSP headers to the most restrictive, so a
    # middleware override must replace CSPMiddleware's value, not ship beside it.
    class Override(Middleware):
        async def process_response(self, request, response):
            response.headers["content-security-policy"] = "default-src *"
            return response

    app = Veloce(openapi_url=None)
    app.add_middleware(Override)
    app.add_middleware(CSPMiddleware, policy="default-src 'self'")

    @app.get("/")
    async def home():
        return JSONResponse({"x": 1})

    resp = app.test_client().get("/")
    csp = _csp_lines(resp.raw_headers)
    assert len(csp) == 1
    assert csp[0][1] == b"default-src *"


def test_asgi_keeps_every_set_cookie():
    app = Veloce(openapi_url=None)

    @app.get("/")
    async def home():
        resp = JSONResponse({"x": 1})
        resp.set_cookie("a", "1")
        resp.set_cookie("b", "2")
        return resp

    raw = app.test_client().get("/").raw_headers
    assert len([1 for k, _ in raw if k.decode().lower() == "set-cookie"]) == 2
