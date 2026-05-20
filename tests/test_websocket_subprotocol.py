"""WebSocket subprotocol negotiation tests (W9)."""

from __future__ import annotations

from veloce.websocket import WebSocket


class _FakeTransport:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def close(self) -> None:
        pass


def _ws(protocol_header: str | None = None) -> WebSocket:
    headers: dict[str, str] = {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ=="}
    if protocol_header is not None:
        headers["sec-websocket-protocol"] = protocol_header
    return WebSocket(_FakeTransport(), headers)


# ── requested_subprotocols ────────────────────────────────────────────


def test_requested_subprotocols_single():
    assert _ws("graphql-ws").requested_subprotocols == ["graphql-ws"]


def test_requested_subprotocols_multiple_in_order():
    """Client preference order is preserved — RFC 6455 §1.9."""
    ws = _ws("graphql-ws, graphql-transport-ws, json.rpc")
    assert ws.requested_subprotocols == [
        "graphql-ws",
        "graphql-transport-ws",
        "json.rpc",
    ]


def test_requested_subprotocols_strips_whitespace():
    ws = _ws("  a  ,  b  ,c")
    assert ws.requested_subprotocols == ["a", "b", "c"]


def test_requested_subprotocols_skips_empty_tokens():
    ws = _ws("a, , b,")
    assert ws.requested_subprotocols == ["a", "b"]


def test_requested_subprotocols_empty_header_returns_empty_list():
    assert _ws("").requested_subprotocols == []
    assert _ws(None).requested_subprotocols == []


# ── negotiate_subprotocol ─────────────────────────────────────────────


def test_negotiate_returns_first_matching_in_client_order():
    """`supported` is the server's allow-list; the client's offer order
    decides which one wins."""
    ws = _ws("v3, v2, v1")
    # Server supports v1 and v2 only; client prefers v2 (its second choice).
    assert ws.negotiate_subprotocol(["v1", "v2"]) == "v2"


def test_negotiate_returns_none_when_no_overlap():
    ws = _ws("v3, v4")
    assert ws.negotiate_subprotocol(["v1", "v2"]) is None


def test_negotiate_returns_none_when_client_offered_nothing():
    ws = _ws(None)
    assert ws.negotiate_subprotocol(["v1", "v2"]) is None


def test_negotiate_case_sensitive_comparison():
    """RFC 6455 protocol tokens are case-sensitive."""
    ws = _ws("MQTT")
    assert ws.negotiate_subprotocol(["mqtt"]) is None
    assert ws.negotiate_subprotocol(["MQTT"]) == "MQTT"


def test_negotiate_when_supported_is_empty():
    ws = _ws("graphql-ws")
    assert ws.negotiate_subprotocol([]) is None
