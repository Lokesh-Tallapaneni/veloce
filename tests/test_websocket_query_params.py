"""WebSocket.query_params / WebSocket.url accessors."""

from __future__ import annotations

from veloce.websocket import WebSocket


def _ws(query: bytes = b"", path: str = "/ws") -> WebSocket:
    scope = {"type": "websocket", "path": path, "query_string": query}
    ws = WebSocket.from_asgi(scope, None, None)
    return ws


def test_query_params_empty_when_no_query():
    ws = _ws()
    assert len(ws.query_params) == 0


def test_query_params_parses_handshake_query():
    ws = _ws(b"token=abc&room=lobby")
    assert ws.query_params["token"] == "abc"
    assert ws.query_params["room"] == "lobby"


def test_query_params_multi_value():
    ws = _ws(b"tag=x&tag=y")
    assert ws.query_params.getlist("tag") == ["x", "y"]


def test_url_without_query():
    ws = _ws(path="/chat")
    assert ws.url == "/chat"


def test_url_with_query():
    ws = _ws(b"token=t", path="/chat")
    assert ws.url == "/chat?token=t"
