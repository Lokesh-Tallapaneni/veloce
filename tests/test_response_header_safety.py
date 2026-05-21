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
