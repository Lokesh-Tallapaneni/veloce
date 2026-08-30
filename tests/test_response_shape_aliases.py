"""Response + Request attribute aliases."""

from __future__ import annotations

from tests._asgi_drive import http_scope
from tests.conftest import make_request
from veloce import Response
from veloce.http.response import StreamingResponse

# ── Response.content_length ──────────────────────────────────────────


def test_content_length_matches_body():
    assert Response(body=b"hello").content_length == 5
    assert Response().content_length == 0


# ── Response.is_streamed ─────────────────────────────────────────────


def test_is_streamed_false_for_buffered():
    assert Response(body=b"x").is_streamed is False


def test_is_streamed_true_for_streaming_response():

    async def gen():
        yield b"x"

    sr = StreamingResponse(content=gen())
    assert sr.is_streamed is True


# ── Response.calculate_content_length ────────────────────────────────


def test_calculate_content_length_writes_header():
    resp = Response(body=b"hello world")
    n = resp.calculate_content_length()
    assert n == 11
    assert resp.headers["Content-Length"] == "11"


# ── Request.environ ──────────────────────────────────────────────────


def test_request_environ_returns_scope():
    scope = http_scope(type="http", method="GET", path="/")
    req = make_request(method="GET", path="/", query_string="", headers={}, body=b"", scope=scope)
    # Same dict object — middleware can read everything through this alias.
    assert req.environ is scope


def test_request_environ_empty_dict_when_no_scope():
    req = make_request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.environ == {}
