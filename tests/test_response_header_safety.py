"""Response-layer header-injection safety — CR/LF rejection."""

from __future__ import annotations

import pytest

from veloce import RedirectResponse, Response


def test_set_cookie_rejects_crlf_in_value():
    resp = Response(body=b"")
    with pytest.raises(ValueError):
        resp.set_cookie("sid", "abc\r\nSet-Cookie: evil=1")


def test_set_cookie_rejects_crlf_in_name():
    resp = Response(body=b"")
    with pytest.raises(ValueError):
        resp.set_cookie("bad\r\nname", "v")


def test_encode_rejects_crlf_in_header_value():
    resp = Response(body=b"ok", headers={"X-Test": "value\r\nInjected: 1"})
    with pytest.raises(ValueError):
        resp.encode()


def test_encode_emits_multiple_set_cookies_as_separate_lines():
    resp = Response(body=b"ok")
    resp.set_cookie("a", "1")
    resp.set_cookie("b", "2")
    assert resp.encode().count(b"Set-Cookie: ") == 2


def test_redirect_response_rejects_crlf_in_url():
    with pytest.raises(ValueError):
        RedirectResponse("https://example.com/\r\nInjected: 1")


def test_redirect_response_percent_encodes_url():
    resp = RedirectResponse("https://example.com/a b")
    assert resp.headers["Location"] == "https://example.com/a%20b"


def test_asgi_emit_rejects_crlf_in_header_value():
    """The ASGI emit path (not just encode()) rejects a CRLF header value."""
    from veloce import Request, Veloce

    app = Veloce(openapi_url=None)

    @app.get("/x")
    async def x(request: Request):
        resp = Response(body=b"ok")
        resp.headers["X-Evil"] = "value\r\nInjected: 1"
        return resp

    with pytest.raises(ValueError):
        app.test_client().get("/x")


async def test_eventsource_stream_to_rejects_crlf_header():
    """EventSourceResponse.stream_to rejects a CRLF header value."""
    from veloce import EventSourceResponse

    async def _empty():
        return
        yield b""  # pragma: no cover - makes this an async generator

    resp = EventSourceResponse(_empty(), headers={"X-Evil": "v\r\nInjected: 1"})

    class _Transport:
        def write(self, data: bytes) -> None:
            pass

    with pytest.raises(ValueError):
        await resp.stream_to(_Transport())


def _count_ci_header(head: bytes, name: str) -> int:
    """Count header lines whose name equals `name` case-insensitively."""
    target = name.lower()
    count = 0
    for raw in head.split(b"\r\n"):
        if b":" not in raw:
            continue
        key = raw.split(b":", 1)[0].decode("latin-1").strip().lower()
        if key == target:
            count += 1
    return count


def test_streaming_response_lowercase_content_type_not_duplicated():
    """A lowercase `content-type` override must not produce two
    Content-Type header lines on the raw-transport encode."""
    from veloce import StreamingResponse

    async def _gen():
        yield b"row\n"

    resp = StreamingResponse(_gen(), headers={"content-type": "text/csv"})
    head = resp.encode()
    assert _count_ci_header(head, "content-type") == 1
    assert b"content-type: text/csv\r\n" in head
    assert b"Content-Type: application/octet-stream\r\n" not in head


def test_streaming_response_lowercase_connection_not_duplicated():
    """A lowercase `connection` override suppresses the default."""
    from veloce import StreamingResponse

    async def _gen():
        yield b"row\n"

    resp = StreamingResponse(_gen(), headers={"connection": "close"})
    head = resp.encode()
    assert _count_ci_header(head, "connection") == 1
    assert b"connection: close\r\n" in head


def test_streaming_response_rejects_crlf_in_content_type():
    """A CRLF-laced `content_type` must not inject an extra header line.

    `content_type` is a public constructor argument that flows into the
    default headers; the raw-transport encode must reject it rather than
    emit `Content-Type: text/csv\\r\\nEvil: 1` (HTTP response splitting)."""
    from veloce import StreamingResponse

    async def _gen():
        yield b"row\n"

    resp = StreamingResponse(_gen(), content_type="text/csv\r\nEvil: 1")
    with pytest.raises(ValueError):
        resp.encode()


def test_streaming_response_rejects_nul_in_content_type():
    """A NUL in `content_type` is rejected on the raw-transport encode."""
    from veloce import StreamingResponse

    async def _gen():
        yield b"row\n"

    resp = StreamingResponse(_gen(), content_type="text/csv\x00")
    with pytest.raises(ValueError):
        resp.encode()


async def test_eventsource_stream_to_lowercase_content_type_not_duplicated():
    """EventSourceResponse must emit one Content-Type even when the
    caller supplies a lowercase `content-type`."""
    from veloce import EventSourceResponse

    async def _empty():
        return
        yield b""  # pragma: no cover - makes this an async generator

    resp = EventSourceResponse(_empty(), headers={"content-type": "text/plain"})

    class _Transport:
        def __init__(self) -> None:
            self.chunks: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.chunks.append(data)

        def writelines(self, data) -> None:
            self.chunks.extend(data)

    transport = _Transport()
    await resp.stream_to(transport)
    head = transport.chunks[0]
    assert _count_ci_header(head, "content-type") == 1
    assert b"content-type: text/plain\r\n" in head
    assert b"Content-Type: text/event-stream\r\n" not in head
