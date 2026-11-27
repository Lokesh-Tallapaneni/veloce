"""Who the connecting peer is, on every transport.

`client_host` / `client_port` are read by anything that needs to identify a
caller - access logs, `remote_addr`, and the rate limiter's per-client bucket.
Two transports supply the peer differently: the native server hands it over on
an `asyncio` transport, an ASGI server puts it on the scope. Both must answer,
because a property that resolves on only one of them fails silently on the
other rather than raising.
"""

from __future__ import annotations

from tests.conftest import make_request
from veloce import Veloce
from veloce.http.request import Request
from veloce.middleware.security import RateLimitMiddleware


class _Transport:
    """Minimal stand-in for the native server's asyncio transport."""

    def __init__(self, peername: tuple | None) -> None:
        self._peername = peername

    def get_extra_info(self, name: str):
        return self._peername if name == "peername" else None


def _request(*, transport=None, scope=None, headers=None) -> Request:
    return make_request(
        method="GET",
        path="/",
        query_string="",
        headers=headers or [],
        body=b"",
        transport=transport,
        scope=scope,
    )


# ── The ASGI transport ───────────────────────────────────────────────


def test_the_peer_is_read_from_the_asgi_scope():
    """The regression this guards: `transport` is always None under ASGI."""
    request = _request(scope={"client": ("203.0.113.7", 54321)})
    assert request.client_host == "203.0.113.7"
    assert request.client_port == 54321


def test_remote_addr_answers_on_the_asgi_scope():
    """`remote_addr` is the alias most access logs read; it must not be None."""
    request = _request(scope={"client": ("203.0.113.7", 54321)})
    assert request.remote_addr == "203.0.113.7"


def test_client_address_answers_on_the_asgi_scope():
    request = _request(scope={"client": ("203.0.113.7", 54321)})
    assert request.client is not None
    assert (request.client.host, request.client.port) == ("203.0.113.7", 54321)


def test_access_route_ends_with_the_asgi_peer():
    request = _request(scope={"client": ("203.0.113.7", 54321)})
    assert request.access_route == ["203.0.113.7"]


# ── The native transport ─────────────────────────────────────────────


def test_the_native_transport_peer_still_wins():
    request = _request(transport=_Transport(("10.0.0.4", 4242)))
    assert request.client_host == "10.0.0.4"
    assert request.client_port == 4242


def test_the_transport_is_preferred_over_the_scope():
    """A request carrying both reports the live connection, not the scope."""
    request = _request(
        transport=_Transport(("10.0.0.4", 4242)),
        scope={"client": ("203.0.113.7", 54321)},
    )
    assert request.client_host == "10.0.0.4"
    assert request.client_port == 4242


# ── ProxyFix keeps precedence ────────────────────────────────────────


def test_a_trusted_proxy_hop_outranks_both_sources():
    request = _request(
        transport=_Transport(("10.0.0.4", 4242)),
        scope={"client": ("203.0.113.7", 54321)},
    )
    request.state["proxy_fix_client"] = "198.51.100.9"
    assert request.client_host == "198.51.100.9"
    assert request.remote_addr == "198.51.100.9"


def test_a_trusted_hop_does_not_rewrite_the_peer_port():
    """Only the host is proxied; the port still describes the real connection."""
    request = _request(scope={"client": ("203.0.113.7", 54321)})
    request.state["proxy_fix_client"] = "198.51.100.9"
    assert request.client_host == "198.51.100.9"
    assert request.client_port == 54321


# ── No peer at all ───────────────────────────────────────────────────


def test_a_synthetic_request_reports_no_peer():
    request = _request()
    assert request.client_host is None
    assert request.client_port is None
    assert request.client is None
    assert request.remote_addr is None
    assert request.access_route == []


def test_a_scope_without_a_client_reports_no_peer():
    request = _request(scope={"type": "http"})
    assert request.client_host is None
    assert request.client is None


def test_a_null_scope_client_reports_no_peer():
    """ASGI allows `client` to be absent or None for a synthetic call."""
    request = _request(scope={"client": None})
    assert request.client_host is None
    assert request.client_port is None


def test_a_transport_with_no_peername_falls_through_to_the_scope():
    request = _request(transport=_Transport(None), scope={"client": ("203.0.113.7", 54321)})
    assert request.client_host == "203.0.113.7"


# ── Malformed peers must not raise ───────────────────────────────────


def test_a_host_only_scope_client_yields_no_port():
    """A one-element peer is malformed; report the host, not an IndexError."""
    request = _request(scope={"client": ("203.0.113.7",)})
    assert request.client_host == "203.0.113.7"
    assert request.client_port is None


def test_a_host_only_peer_still_builds_an_address():
    request = _request(scope={"client": ("203.0.113.7",)})
    assert request.client is not None
    assert (request.client.host, request.client.port) == ("203.0.113.7", 0)


def test_an_empty_scope_client_reports_no_peer():
    request = _request(scope={"client": ()})
    assert request.client_host is None
    assert request.client_port is None


# ── The forwarded chain ──────────────────────────────────────────────


def test_the_forwarded_chain_appends_the_asgi_peer():
    request = _request(
        scope={"client": ("203.0.113.7", 54321)},
        headers=[(b"x-forwarded-for", b"198.51.100.1, 198.51.100.2")],
    )
    assert request.access_route == ["198.51.100.1", "198.51.100.2", "203.0.113.7"]


# ── The rate limiter buckets by IP again ─────────────────────────────


def test_the_rate_limiter_buckets_an_asgi_caller_by_address():
    """The security consequence: without the scope peer the limiter fell back to
    a User-Agent hash, so varying one header defeated it entirely."""

    limiter = RateLimitMiddleware(max_requests=1, window_seconds=60)
    same_ip_a = _request(
        scope={"client": ("203.0.113.7", 1)}, headers=[(b"user-agent", b"agent-a")]
    )
    same_ip_b = _request(
        scope={"client": ("203.0.113.7", 2)}, headers=[(b"user-agent", b"agent-b")]
    )
    other_ip = _request(
        scope={"client": ("198.51.100.9", 1)}, headers=[(b"user-agent", b"agent-a")]
    )

    # One address is one bucket, whatever the client calls itself.
    assert limiter._bucket_key(same_ip_a) == limiter._bucket_key(same_ip_b)
    assert limiter._bucket_key(same_ip_a) != limiter._bucket_key(other_ip)
    assert limiter._bucket_key(same_ip_a).startswith("host:")


def test_the_limiter_still_partitions_callers_with_no_address():
    """No peer means no IP to key on; anonymous callers must not share a bucket."""

    limiter = RateLimitMiddleware(max_requests=1, window_seconds=60)
    one = _request(headers=[(b"user-agent", b"agent-a")])
    two = _request(headers=[(b"user-agent", b"agent-b")])
    assert limiter._bucket_key(one) != limiter._bucket_key(two)


# ── End to end through the app ───────────────────────────────────────


def test_a_handler_sees_the_peer_through_the_test_client():
    app = Veloce(title="Peer", openapi_url=None)

    @app.get("/who")
    async def who(request: Request) -> dict:
        return {"host": request.client_host, "addr": request.remote_addr}

    with app.test_client() as client:
        body = client.get("/who").json()
    assert body["host"] is not None
    assert body["host"] == body["addr"]
