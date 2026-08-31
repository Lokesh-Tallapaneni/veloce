"""A streamed response is framed the way a buffered one is.

Three rules the buffered emit had always applied were missing from its
streaming twins, and each put bytes on the wire that contradict the headers
beside them:

- a bodiless status (1xx / 204 / 205 / 304) carries no payload and no framing.
  `StreamingResponse(status_code=204)` shipped its chunks and advertised
  `Transfer-Encoding`, which RFC 9112 Sec. 6.1 forbids outright on a 204. On a
  keep-alive connection the client reads those bytes as the next response.
- `Content-Length` follows the body. `set_data` refreshed it; assigning
  `response.body` - documented as the same thing - did not, so a middleware
  rewriting a body advertised the old length.
- a HEAD returns the header section a GET would have produced (RFC 9110
  Sec. 9.3.2). `EventSourceResponse` extends `Response`, not
  `StreamingResponse`, so it inherited the buffered `encode()` and a native
  HEAD advertised `Content-Length: 0` for a resource that streams forever.
"""

from __future__ import annotations

import pytest

from tests._asgi_drive import body_of, drive, headers_of, status_of
from veloce import Response, Veloce
from veloce.http.response import StreamingResponse
from veloce.sse import EventSourceResponse, ServerSentEvent

_BODILESS = [204, 205, 304]


async def _chunks():
    yield b"SHOULD-NOT-BE-SENT"


def _stream(**kw) -> StreamingResponse:
    return StreamingResponse(_chunks(), content_type="text/plain", **kw)


async def _drive(app: Veloce, path: str, method: str = "GET"):
    """Status, headers and body of one request, off the raw message stream."""
    messages = await drive(app, path=path, method=method)
    return status_of(messages), headers_of(messages), body_of(messages)


# ── A bodiless status carries nothing, on either transport ───────────


@pytest.mark.parametrize("status", _BODILESS)
def test_a_streamed_bodiless_status_sends_no_body_natively(status):
    """The defect: the chunks went out and desynchronised the connection."""
    head = _stream(status_code=status).encode()
    assert b"SHOULD-NOT-BE-SENT" not in head
    assert b"Transfer-Encoding" not in head


@pytest.mark.parametrize("status", _BODILESS)
async def test_a_streamed_bodiless_status_sends_no_body_over_asgi(status):
    app = Veloce(openapi_url=None)

    @app.get("/s")
    async def s():
        return _stream(status_code=status)

    got_status, headers, body = await _drive(app, "/s")
    assert got_status == status
    assert body == b""
    assert "transfer-encoding" not in headers
    assert "content-type" not in headers


@pytest.mark.parametrize("status", _BODILESS)
def test_the_two_emit_paths_agree_on_a_bodiless_status(status):
    """Whatever the rule is, the buffered and streamed heads must state it alike."""
    buffered = Response(status_code=status, body=b"X", content_type="text/plain").encode()
    streamed = _stream(status_code=status).encode()
    assert (b"Transfer-Encoding" in buffered) == (b"Transfer-Encoding" in streamed)
    assert (b"Content-Type" in buffered) == (b"Content-Type" in streamed)


def test_a_streamed_ok_still_streams():
    """The rule must not cost a normal streamed response its framing."""
    head = _stream(status_code=200).encode()
    assert b"Transfer-Encoding: chunked" in head
    assert b"Content-Type: text/plain" in head


# ── Content-Length follows the body ──────────────────────────────────


def _with_length(body: bytes = b"0123456789") -> Response:
    response = Response(body=body, content_type="text/plain")
    response.calculate_content_length()
    return response


def test_assigning_body_refreshes_content_length():
    """The defect: the obvious spelling advertised the previous length."""
    response = _with_length()
    response.body = b"hi"
    assert response.headers["Content-Length"] == "2"
    assert response.encode().endswith(b"\r\n\r\nhi")


def test_assigning_body_and_set_data_agree():
    """`data` is documented as an alias; it must behave as one."""
    a, b = _with_length(), _with_length()
    a.set_data(b"hi")
    b.body = b"hi"
    assert a.headers == b.headers
    assert a.encode() == b.encode()


def test_assigning_body_does_not_invent_a_content_length():
    """A response that never advertised one must not start."""
    response = Response(body=b"0123456789", content_type="text/plain")
    response.body = b"hi"
    assert "Content-Length" not in response.headers


@pytest.mark.parametrize("spelling", ["Content-Length", "content-length"])
def test_the_refresh_finds_the_header_under_either_casing(spelling):
    response = Response(body=b"0123456789", headers={spelling: "10"})
    response.body = b"hi"
    assert response.headers[spelling] == "2"
    assert len(response.headers) == 1


def test_a_body_rewriting_middleware_advertises_the_new_length():
    """The shape that made this a wire defect rather than a papercut."""
    response = _with_length(b"a" * 100)
    response.body = b"compressed"
    encoded = response.encode()
    head, _, body = encoded.partition(b"\r\n\r\n")
    assert b"Content-Length: 10" in head
    assert body == b"compressed"


def test_a_304_still_advertises_its_representation_length():
    """RFC 9110 Sec. 8.6: a 304 may state the would-be-200 length.

    The body is emptied on the way to a 304, which now also refreshes any
    `Content-Length` - so the representation length it records afterwards must
    survive that.
    """
    response = Response(body=b"0123456789", content_type="text/plain")
    response._downgrade_to_304()
    head = response.encode()
    assert response.status_code == 304
    assert b"Content-Length: 10" in head
    assert head.endswith(b"\r\n\r\n")


# ── HEAD says what GET would say ─────────────────────────────────────


def test_an_sse_head_advertises_the_framing_its_get_uses():
    """The defect: HEAD said `Content-Length: 0` for an endless stream."""

    async def events():
        yield ServerSentEvent.json({"a": 1})

    head = EventSourceResponse(events()).encode()
    assert b"Transfer-Encoding: chunked" in head
    assert b"Content-Length" not in head
    assert b"Content-Type: text/event-stream" in head


async def test_an_sse_get_still_streams_its_events():
    app = Veloce(openapi_url=None)

    @app.get("/sse")
    async def sse():
        async def events():
            yield ServerSentEvent.json({"a": 1})

        return EventSourceResponse(events())

    _status, headers, body = await _drive(app, "/sse")
    assert headers["content-type"].startswith("text/event-stream")
    assert b'{"a":1}' in body


async def test_an_sse_head_sends_no_events():
    app = Veloce(openapi_url=None)

    @app.get("/sse")
    async def sse():
        async def events():
            yield ServerSentEvent.json({"a": 1})

        return EventSourceResponse(events())

    _status, _headers, body = await _drive(app, "/sse", method="HEAD")
    assert body == b""
