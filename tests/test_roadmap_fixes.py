"""Regression tests for the roadmap true-positive batch.

- **C3** — `receive_text` / `receive_bytes` / `receive_json` now refuse
  to run before `accept()` and raise on send/receive after close, the
  same handshake-state machine the `send_*` siblings already enforce.
- **S2** — `WebSocket.origin` exposes the handshake `Origin` header and
  `WebSocket.check_origin(allowed)` returns `True` only when the origin
  is on the allow-list. Designed to be called *before* `accept()` so
  handlers can reject Cross-Site WebSocket Hijacking with a `close(1008)`.
- **P-3** — `Response` is imported at module-load time in
  `veloce.dependency`, not on every Response-injecting request. This
  test pins the module-level binding so a future refactor cannot
  silently revert to the inline import.
"""

from __future__ import annotations

import asyncio

import pytest

from veloce.websocket import WebSocket


class _FakeTransport:
    """Minimal asyncio-transport stand-in for direct WebSocket tests."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        self.closed = True

    def get_extra_info(self, key: str) -> None:
        return None


# ── C3 — receive-side state-machine guards ───────────────────────────


def test_receive_text_before_accept_raises():
    """Calling `receive_text` before `accept()` is a programming error
    — without the guard the caller hung on the empty queue forever."""

    async def go() -> None:
        ws = WebSocket(_FakeTransport(), {})
        with pytest.raises(RuntimeError, match="call accept"):
            await ws.receive_text(timeout=0.01)

    asyncio.run(go())


def test_receive_bytes_before_accept_raises():
    async def go() -> None:
        ws = WebSocket(_FakeTransport(), {})
        with pytest.raises(RuntimeError, match="call accept"):
            await ws.receive_bytes(timeout=0.01)

    asyncio.run(go())


def test_receive_json_before_accept_raises():
    """`receive_json` routes through `receive_text`, so it inherits the
    guard — pin the behaviour so a future refactor cannot regress it."""

    async def go() -> None:
        ws = WebSocket(_FakeTransport(), {})
        with pytest.raises(RuntimeError, match="call accept"):
            await ws.receive_json(timeout=0.01)

    asyncio.run(go())


def test_receive_after_close_raises_disconnect():
    """A receive after the application closed the connection is a
    `WebSocketDisconnect`, matching the `send_*` close-state behaviour."""
    from veloce.exceptions import WebSocketDisconnect

    async def go() -> None:
        ws = WebSocket(_FakeTransport(), {})
        ws._accepted = True
        ws._closed = True
        with pytest.raises(WebSocketDisconnect):
            await ws.receive_text(timeout=0.01)

    asyncio.run(go())


# ── S2 — Origin accessor + check_origin helper ──────────────────────


def test_websocket_origin_reads_handshake_header():
    ws = WebSocket(_FakeTransport(), {"origin": "https://app.example.com"})
    assert ws.origin == "https://app.example.com"


def test_websocket_origin_is_none_when_header_absent():
    """A non-browser client may legitimately omit the `Origin` header."""
    ws = WebSocket(_FakeTransport(), {})
    assert ws.origin is None


def test_check_origin_single_allowed_origin_match():
    ws = WebSocket(_FakeTransport(), {"origin": "https://app.example.com"})
    assert ws.check_origin("https://app.example.com") is True


def test_check_origin_rejects_unlisted_origin():
    """The classic CSWSH defence — an attacker-controlled origin must
    not pass the allow-list check, even when the handshake otherwise
    looks well-formed."""
    ws = WebSocket(_FakeTransport(), {"origin": "https://evil.example.com"})
    assert ws.check_origin("https://app.example.com") is False
    assert ws.check_origin(["https://app.example.com", "https://admin.example.com"]) is False


def test_check_origin_iterable_of_allowed():
    ws = WebSocket(_FakeTransport(), {"origin": "https://admin.example.com"})
    assert ws.check_origin(["https://app.example.com", "https://admin.example.com"]) is True


def test_check_origin_is_case_insensitive():
    """Per RFC 6454 §4, scheme/host comparison is case-insensitive."""
    ws = WebSocket(_FakeTransport(), {"origin": "HTTPS://App.Example.COM"})
    assert ws.check_origin("https://app.example.com") is True


def test_check_origin_missing_header_returns_false():
    """No `Origin` header → the check fails closed; the handler can
    branch on `ws.origin is None` explicitly to allow non-browser
    clients."""
    ws = WebSocket(_FakeTransport(), {})
    assert ws.check_origin("https://app.example.com") is False
    assert ws.check_origin(["https://app.example.com"]) is False


# ── P-3 — Response imported at module load, not per-request ────────


def test_response_import_is_module_level_in_dependency():
    """The Response symbol must be bound on the dependency module at
    import time. The previous inline `from veloce.http.response import
    Response` inside `_resolve_slots` paid an import-system lookup on
    every request whose handler injected a Response."""
    import veloce.dependency as dep
    from veloce.http.response import Response

    assert hasattr(dep, "Response")
    assert dep.Response is Response
