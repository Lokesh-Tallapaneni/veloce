"""Response + Request attribute aliases."""

from __future__ import annotations

from veloce import Request, Response

# ── Response.content_length ──────────────────────────────────────────


def test_content_length_matches_body():
    assert Response(body=b"hello").content_length == 5
    assert Response().content_length == 0


# ── Response.is_streamed ─────────────────────────────────────────────


def test_is_streamed_false_for_buffered():
    assert Response(body=b"x").is_streamed is False


def test_is_streamed_true_for_streaming_response():
    from veloce.http.response import StreamingResponse

    async def gen():
        yield b"x"

    sr = StreamingResponse(content=gen())
    assert sr.is_streamed is True


# ── Response.charset ─────────────────────────────────────────────────


def test_charset_defaults_to_utf8():
    assert Response().charset == "utf-8"


def test_charset_from_content_type_parameter():
    resp = Response(content_type="text/html; charset=iso-8859-1")
    assert resp.charset == "iso-8859-1"


def test_charset_handles_quoted_value():
    resp = Response(content_type='text/html; charset="windows-1252"')
    assert resp.charset == "windows-1252"


# ── Response.calculate_content_length ────────────────────────────────


def test_calculate_content_length_writes_header():
    resp = Response(body=b"hello world")
    n = resp.calculate_content_length()
    assert n == 11
    assert resp.headers["Content-Length"] == "11"


# ── Request.environ ──────────────────────────────────────────────────


def test_request_environ_returns_scope():
    scope = {"type": "http", "method": "GET", "path": "/"}
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"", scope=scope)
    # Same dict object — middleware can read everything through this alias.
    assert req.environ is scope


def test_request_environ_empty_dict_when_no_scope():
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.environ == {}
