"""Non-latin-1 response header values: RFC 2047 MIME-encoding on both paths.

HTTP header values are emitted as latin-1. A value outside latin-1 used to
raise `UnicodeEncodeError` on the HTTP/1.1 path and emit raw UTF-8 (mojibake)
on the ASGI path. Both now MIME-encode to an ASCII `=?utf-8?b?...?=` token via
`_encode_header_value`; ASCII and latin-1 values are emitted verbatim.
"""

from __future__ import annotations

import base64

from veloce import Response, Veloce
from veloce._internal import _encode_header_value, _encode_response_head

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


# ── the encoded token must never carry a newline ─────────────────────
#
# `_encode_header_value` promised no fold, and `maxlinelen=sys.maxsize` did
# suppress *length*-based folding - but `email.header.Header.encode()` also
# folds on any character Python counts as a line break. A value carrying U+2028
# or U+2029 therefore came back as two Q-encoded words joined by a bare LF:
#
#     'a\u2028b' -> '=?utf-8?q?a?=\n =?utf-8?q?_b?='
#
# which is obs-fold, and RFC 9112 Sec. 5.2 says a sender MUST NOT generate it.
# `_reject_header_crlf` does not catch these: they are not CR, LF or NUL.


def test_a_line_separator_does_not_fold_the_encoded_value():
    """NEGATIVE: U+2028 must not put a bare LF into the header bytes."""
    encoded = _encode_header_value("a\u2028b")

    assert "\n" not in encoded
    assert "\r" not in encoded


def test_a_paragraph_separator_does_not_fold_the_encoded_value():
    """NEGATIVE: U+2029 folds the same way and must not either."""
    encoded = _encode_header_value("a\u2029b")

    assert "\n" not in encoded
    assert "\r" not in encoded


def test_no_line_break_character_can_fold_the_encoded_value():
    """NEGATIVE: every break `str.splitlines()` honours, not just the two found.

    Fixing the reported pair and leaving the rest would repeat the defect: the
    guarantee is about the whole class, not the instances someone happened to
    try.
    """
    for codepoint in ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
        encoded = _encode_header_value(f"a{codepoint}b\U0001f600")
        assert "\n" not in encoded, repr(codepoint)
        assert "\r" not in encoded, repr(codepoint)


def test_the_encoded_token_still_decodes_to_the_original_value():
    """POSITIVE: refusing to fold is worthless if the value is lost."""
    original = "a\u2028b\U0001f600"
    encoded = _encode_header_value(original)

    assert encoded.startswith("=?utf-8?b?")
    assert encoded.endswith("?=")
    assert base64.b64decode(encoded[len("=?utf-8?b?") : -len("?=")]).decode("utf-8") == original


def test_an_ascii_value_is_still_returned_verbatim():
    """POSITIVE: the fast path must not start encoding."""
    assert _encode_header_value("plain-value") == "plain-value"


def test_a_latin_1_value_is_still_returned_verbatim():
    """POSITIVE: latin-1 is wire-representable and must pass through."""
    assert _encode_header_value("café") == "café"


def test_a_response_with_a_folding_value_emits_one_header_line():
    """NEGATIVE: end to end - the emitted head must carry no stray newline."""
    lines = _encode_response_head(200, {}, {"X-Echo": "a\u2028b"}, keep_alive=True)

    # Each element is one line ending in CRLF; a fold would put a newline
    # *inside* one, which is the obs-fold RFC 9112 Sec. 5.2 forbids emitting.
    for line in lines:
        body = line[:-2] if line.endswith("\r\n") else line
        assert "\n" not in body, line
        assert "\r" not in body, line
