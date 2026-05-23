"""S2 — WebSocket Origin accessor + `check_origin` helper.

The WebSocket handshake is plain HTTP/1.1; Same-Origin Policy and CORS
do not apply, so Cross-Site WebSocket Hijacking is the canonical
attack. `WebSocket.check_origin(allowed)` is the per-handler defence,
parallel to the registered-once `WebSocketOriginMiddleware`. The two
APIs share normalisation (`.rstrip("/").lower()`), wildcard semantics
(`"*"` accepts any origin), and a strict-by-default treatment of
missing `Origin` headers.
"""

from __future__ import annotations

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


# ── Origin accessor ─────────────────────────────────────────────────


def test_origin_reads_handshake_header():
    ws = WebSocket(_FakeTransport(), {"origin": "https://app.example.com"})
    assert ws.origin == "https://app.example.com"


def test_origin_is_none_when_header_absent():
    """A non-browser client may legitimately omit the `Origin` header."""
    ws = WebSocket(_FakeTransport(), {})
    assert ws.origin is None


# ── check_origin happy paths ────────────────────────────────────────


def test_check_origin_single_allowed_origin_match():
    ws = WebSocket(_FakeTransport(), {"origin": "https://app.example.com"})
    assert ws.check_origin("https://app.example.com") is True


def test_check_origin_iterable_of_allowed():
    ws = WebSocket(_FakeTransport(), {"origin": "https://admin.example.com"})
    assert ws.check_origin(["https://app.example.com", "https://admin.example.com"]) is True


def test_check_origin_is_case_insensitive():
    """Per RFC 6454 §4, scheme/host comparison is case-insensitive."""
    ws = WebSocket(_FakeTransport(), {"origin": "HTTPS://App.Example.COM"})
    assert ws.check_origin("https://app.example.com") is True


def test_check_origin_strips_trailing_slash_on_both_sides():
    """Normalisation matches `WebSocketOriginMiddleware`
    (`.rstrip("/").lower()`), so allow-lists are interchangeable
    between the two APIs."""
    ws = WebSocket(_FakeTransport(), {"origin": "https://app.example.com/"})
    assert ws.check_origin("https://app.example.com") is True
    ws2 = WebSocket(_FakeTransport(), {"origin": "https://app.example.com"})
    assert ws2.check_origin("https://app.example.com/") is True


# ── check_origin defensive paths ────────────────────────────────────


def test_check_origin_rejects_unlisted_origin():
    """The classic CSWSH defence — an attacker-controlled origin must
    not pass the allow-list check, even when the handshake otherwise
    looks well-formed."""
    ws = WebSocket(_FakeTransport(), {"origin": "https://evil.example.com"})
    assert ws.check_origin("https://app.example.com") is False
    assert ws.check_origin(["https://app.example.com", "https://admin.example.com"]) is False


def test_check_origin_missing_header_returns_false():
    """No `Origin` header → the check fails closed; the handler can
    branch on `ws.origin is None` explicitly to allow non-browser
    clients."""
    ws = WebSocket(_FakeTransport(), {})
    assert ws.check_origin("https://app.example.com") is False
    assert ws.check_origin(["https://app.example.com"]) is False


def test_check_origin_null_literal_is_rejected():
    """Browsers send `Origin: null` for sandboxed iframes and `file://`
    pages. That must not match any real origin in the allow-list."""
    ws = WebSocket(_FakeTransport(), {"origin": "null"})
    assert ws.check_origin("https://app.example.com") is False
    assert ws.check_origin(["null", "https://app.example.com"]) is False


# ── Wildcard ─────────────────────────────────────────────────────────


def test_check_origin_wildcard_accepts_any_origin():
    """`"*"` in `allowed` is the opt-in "I have my own check elsewhere"
    escape hatch — symmetric with `WebSocketOriginMiddleware`'s
    `allowed_origins=["*"]`."""
    ws = WebSocket(_FakeTransport(), {"origin": "https://evil.example.com"})
    assert ws.check_origin("*") is True
    assert ws.check_origin(["*"]) is True
    assert ws.check_origin(["https://app.example.com", "*"]) is True


def test_check_origin_wildcard_accepts_missing_origin():
    """Wildcard short-circuits the missing-header branch too — matches
    the middleware's `_allow_all` check."""
    ws = WebSocket(_FakeTransport(), {})
    assert ws.check_origin("*") is True
