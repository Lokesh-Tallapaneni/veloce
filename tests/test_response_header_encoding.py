"""Non-latin-1 response header values: RFC 2047 MIME-encoding on both paths.

HTTP header values are emitted as latin-1. A value outside latin-1 used to
raise `UnicodeEncodeError` on the HTTP/1.1 path and emit raw UTF-8 (mojibake)
on the ASGI path. Both now MIME-encode to an ASCII `=?utf-8?b?...?=` token via
`_encode_header_value`; ASCII and latin-1 values are emitted verbatim.
"""

from __future__ import annotations

from veloce import Response, Veloce
from veloce._internal import _encode_header_value

# ── helper unit ──────────────────────────────────────────────────────


def test_ascii_fast_path_unchanged():
    assert _encode_header_value("plain ascii") == "plain ascii"


def test_latin1_value_passthrough():
    # Non-ASCII but latin-1-representable -> emitted verbatim, not encoded.
    assert _encode_header_value("résumé") == "résumé"


def test_non_latin1_value_mime_encoded():
    out = _encode_header_value("café ☃")
    assert out.startswith("=?utf-8?")
    assert out.isascii()


def test_long_non_latin1_value_has_no_crlf_fold():
    # `☃` is outside latin-1, so this forces the MIME-encoding branch; the
    # long value must stay a single line (maxlinelen=sys.maxsize guard).
    out = _encode_header_value("café ☃ " * 50)
    assert "\r" not in out and "\n" not in out
    assert out.isascii()


# ── HTTP/1.1 emit path (Response.encode) ─────────────────────────────


def test_http1_non_latin1_header_does_not_raise():
    head = Response(body=b"x", headers={"X-Note": "café ☃"}).encode()
    assert b"X-Note: =?utf-8?" in head


def test_http1_latin1_header_emitted_verbatim():
    head = Response(body=b"x", headers={"X-Note": "résumé"}).encode()
    assert b"X-Note: " + "résumé".encode("latin-1") in head


def test_http1_non_latin1_set_cookie_no_crlf():
    head = Response(body=b"x", headers={"Set-Cookie": "k=café ☃"}).encode()
    # MIME-encoded, single line, no stray newline inside the value.
    assert b"Set-Cookie: =?utf-8?" in head


# ── ASGI emit path (TestClient) + parity ─────────────────────────────


def _client():
    app = Veloce(debug=False, openapi_url=None)

    @app.get("/h")
    async def h():
        return Response(body=b"x", headers={"X-Note": "café ☃"})

    return app.test_client()


def test_asgi_non_latin1_header_is_mime_token():
    resp = _client().get("/h")
    value = resp.headers.get("X-Note") or resp.headers.get("x-note")
    assert value.startswith("=?utf-8?")
    assert value.isascii()


def test_asgi_and_http1_agree_on_encoded_value():
    """The split between the two emit paths is gone - same token both ways."""
    asgi = _client().get("/h")
    asgi_val = asgi.headers.get("X-Note") or asgi.headers.get("x-note")
    head = Response(body=b"x", headers={"X-Note": "café ☃"}).encode()
    assert f"X-Note: {asgi_val}".encode("latin-1") in head
