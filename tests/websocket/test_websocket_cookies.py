"""WebSocket.cookies — handshake cookie parsing."""

from __future__ import annotations

from veloce.websocket import WebSocket


def _ws(cookie: str | None = None) -> WebSocket:
    headers = [(b"cookie", cookie.encode())] if cookie else []
    scope = {"type": "websocket", "path": "/ws", "headers": headers}
    return WebSocket.from_asgi(scope, None, None)


def test_cookies_empty_when_no_header():
    assert _ws().cookies == {}


def test_cookies_parsed_from_handshake():
    ws = _ws("session=abc123; theme=dark")
    assert ws.cookies == {"session": "abc123", "theme": "dark"}


def test_cookies_single_pair():
    ws = _ws("token=xyz")
    assert ws.cookies["token"] == "xyz"


def test_cookies_returns_dict():
    assert isinstance(_ws("a=1").cookies, dict)
