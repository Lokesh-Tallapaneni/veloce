"""ProxyFix middleware tests (M10)."""

from __future__ import annotations

import pytest

from veloce import ProxyFix, Request, Veloce
from veloce.testclient import TestClient


def _req(headers: dict[str, str]) -> Request:
    return Request(method="GET", path="/", query_string="", headers=headers, body=b"")


# ── Hop-picking ────────────────────────────────────────────────────────


def test_pick_hop_returns_rightmost_for_one_trusted():
    """`X-Forwarded-For: client, proxy1, proxy2` + trust=1 → proxy2."""
    assert ProxyFix._pick_hop("client, proxy1, proxy2", 1) == "proxy2"


def test_pick_hop_walks_back_for_multiple_trusted():
    """trust=2 → proxy1 (one hop further out from the immediate peer)."""
    assert ProxyFix._pick_hop("client, proxy1, proxy2", 2) == "proxy1"
    assert ProxyFix._pick_hop("client, proxy1, proxy2", 3) == "client"


def test_pick_hop_returns_none_when_chain_shorter():
    """Refuse to fabricate a value if the chain is shorter than the trust."""
    assert ProxyFix._pick_hop("only-one", 2) is None


def test_pick_hop_returns_none_for_zero_or_missing():
    assert ProxyFix._pick_hop("a, b", 0) is None
    assert ProxyFix._pick_hop(None, 1) is None
    assert ProxyFix._pick_hop("", 1) is None


def test_pick_hop_strips_whitespace():
    assert ProxyFix._pick_hop("a,  b ,c", 2) == "b"


# ── Forwarded header (RFC 7239) ────────────────────────────────────────


def test_parse_forwarded_basic():
    pf = ProxyFix(x_for=1, x_proto=1, x_host=1)
    p = pf._parse_forwarded(
        "for=client.example.com; proto=https; host=ex.com",
        x_for=1,
        x_proto=1,
        x_host=1,
        x_prefix=0,
    )
    assert p == {"for": "client.example.com", "proto": "https", "host": "ex.com"}


def test_parse_forwarded_strips_ipv6_brackets():
    pf = ProxyFix(x_for=1)
    p = pf._parse_forwarded(
        'for="[2001:db8::1]:8080"',
        x_for=1,
        x_proto=0,
        x_host=0,
        x_prefix=0,
    )
    assert p["for"] == "2001:db8::1"


def test_parse_forwarded_selects_from_right():
    """Multiple proxies -- select by trust depth from the right, not the left."""
    pf = ProxyFix(x_for=1, x_proto=1)
    p = pf._parse_forwarded(
        "for=attacker; proto=http, for=trusted; proto=https",
        x_for=1,
        x_proto=1,
        x_host=0,
        x_prefix=0,
    )
    assert p["for"] == "trusted"
    assert p["proto"] == "https"


# ── Construction validation ────────────────────────────────────────────


def test_construction_rejects_negative_counts():
    with pytest.raises(ValueError):
        ProxyFix(x_for=-1)


# ── Integration via TestClient ─────────────────────────────────────────


def _make_app_with_proxy_fix(**kwargs) -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(ProxyFix(**kwargs))

    @app.get("/info")
    async def info(request: Request):
        return {
            "client": request.client_host,
            "scheme": request.url.scheme,
            "host": request.url.host,
            "secure": request.is_secure,
        }

    return app


def test_proxy_fix_rewrites_client_ip():
    app = _make_app_with_proxy_fix(x_for=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"X-Forwarded-For": "203.0.113.1, 10.0.0.1"},
    )
    # With x_for=1 we trust ONE hop; the rightmost (10.0.0.1) is our proxy.
    # The original client is one hop in: 203.0.113.1.
    # Wait — we trust _one_ proxy, so we trust _one_ hop-back, returning the
    # rightmost value (the proxy itself). That value would be the peer's
    # known IP. The intent is usually x_for=1 ⇒ get the LAST proxy's
    # asserted client. Trace through _pick_hop("a,b", 1) → "b".
    # So with x_for=1 here, client="10.0.0.1". For original client, use x_for=2.
    assert resp.json()["client"] == "10.0.0.1"


def test_proxy_fix_walks_back_with_higher_trust():
    app = _make_app_with_proxy_fix(x_for=2)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"X-Forwarded-For": "203.0.113.1, 10.0.0.1"},
    )
    # Trust 2 proxies → original client one further back.
    assert resp.json()["client"] == "203.0.113.1"


def test_proxy_fix_rewrites_scheme_to_https():
    app = _make_app_with_proxy_fix(x_proto=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"X-Forwarded-Proto": "https"},
    )
    body = resp.json()
    assert body["scheme"] == "https"
    assert body["secure"] is True


def test_proxy_fix_rewrites_host():
    app = _make_app_with_proxy_fix(x_host=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"X-Forwarded-Host": "public.example.com", "Host": "internal"},
    )
    assert resp.json()["host"] == "public.example.com"


def test_proxy_fix_strips_port_from_client_value():
    """`X-Forwarded-For` values that include `:port` should yield just the
    host portion for `request.client_host`."""
    app = _make_app_with_proxy_fix(x_for=1)
    client = TestClient(app)
    resp = client.get("/info", headers={"X-Forwarded-For": "203.0.113.1:12345"})
    assert resp.json()["client"] == "203.0.113.1"


def test_proxy_fix_honors_rfc_7239_forwarded_header():
    app = _make_app_with_proxy_fix(x_for=1, x_proto=1, x_host=1, trust_forwarded=True)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={
            "Forwarded": "for=192.0.2.43; proto=https; host=example.com",
        },
    )
    body = resp.json()
    assert body["client"] == "192.0.2.43"
    assert body["scheme"] == "https"
    assert body["host"] == "example.com"


def test_proxy_fix_disabled_when_count_zero():
    """If x_for is left at 0 (the default for unconfigured fields), the
    header is ignored — defending against spoofing."""
    app = _make_app_with_proxy_fix(x_for=0, x_proto=0, x_host=0, trust_forwarded=False)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"X-Forwarded-For": "evil.example.com"},
    )
    # No proxy_fix_client was stored; client_host falls back to transport (None).
    assert resp.json()["client"] is None


def test_parse_forwarded_extracts_prefix_extension():
    """RFC 7239 §4 allows extension fields; `prefix=` is a de-facto convention."""
    pf = ProxyFix(x_prefix=1)
    p = pf._parse_forwarded(
        "for=client; prefix=/api",
        x_for=0,
        x_proto=0,
        x_host=0,
        x_prefix=1,
    )
    assert p["prefix"] == "/api"


def _make_app_capturing_prefix(**kwargs) -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(ProxyFix(**kwargs))

    @app.get("/info")
    async def info(request: Request):
        return {"script_root": request.script_root}

    return app


def test_proxy_fix_honors_forwarded_prefix_extension():
    """`Forwarded: prefix=/api` (no X-Forwarded-Prefix) rewrites script_root."""
    app = _make_app_capturing_prefix(x_prefix=1)
    client = TestClient(app)
    resp = client.get("/info", headers={"Forwarded": "for=192.0.2.1; prefix=/api"})
    assert resp.json()["script_root"] == "/api"


def test_proxy_fix_forwarded_prefix_wins_over_x_forwarded_prefix():
    """When both headers are present, `Forwarded` wins -- matching host/proto/for."""
    app = _make_app_capturing_prefix(x_prefix=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={
            "Forwarded": "for=192.0.2.1; prefix=/api",
            "X-Forwarded-Prefix": "/legacy",
        },
    )
    assert resp.json()["script_root"] == "/api"


def test_proxy_fix_falls_back_to_x_forwarded_prefix_when_forwarded_lacks_prefix():
    """If `Forwarded` has no `prefix=`, the X-Forwarded-Prefix header still wins."""
    app = _make_app_capturing_prefix(x_prefix=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={
            "Forwarded": "for=192.0.2.1; proto=https",
            "X-Forwarded-Prefix": "/legacy",
        },
    )
    assert resp.json()["script_root"] == "/legacy"


# ── CRLF / header-injection rejection ─────────────────────────────────
#
# These exercise the middleware directly so we observe the raised
# `ValueError` at the point of injection. Inside the request pipeline the
# same error becomes a 500 (debug) or a generic server error — the
# important contract is that no proxy-supplied control character ever
# lands on `request.script_root`, `request.host`, `request.scheme`, etc.


async def _run_proxy_fix(headers: dict[str, str], **kwargs) -> None:
    pf = ProxyFix(**kwargs)
    await pf.process_request(_req(headers))


async def test_proxy_fix_rejects_crlf_in_forwarded_prefix():
    """`Forwarded: prefix=...` with embedded CRLF must raise — header
    injection would otherwise reach response Location / Set-Cookie via
    request.script_root."""
    with pytest.raises(ValueError):
        await _run_proxy_fix(
            {"Forwarded": 'for=192.0.2.1; prefix="/api\r\nInjected: 1"'},
            x_prefix=1,
        )


async def test_proxy_fix_rejects_crlf_in_x_forwarded_prefix():
    with pytest.raises(ValueError):
        await _run_proxy_fix(
            {"X-Forwarded-Prefix": "/api\r\nInjected: 1"},
            x_prefix=1,
        )


async def test_proxy_fix_rejects_crlf_in_x_forwarded_host():
    with pytest.raises(ValueError):
        await _run_proxy_fix(
            {"X-Forwarded-Host": "evil.example.com\r\nInjected: 1"},
            x_host=1,
        )


async def test_proxy_fix_rejects_crlf_in_x_forwarded_proto():
    with pytest.raises(ValueError):
        await _run_proxy_fix(
            {"X-Forwarded-Proto": "https\r\nInjected: 1"},
            x_proto=1,
        )


async def test_proxy_fix_rejects_crlf_in_x_forwarded_for():
    with pytest.raises(ValueError):
        await _run_proxy_fix(
            {"X-Forwarded-For": "203.0.113.1\r\nInjected: 1"},
            x_for=1,
        )


async def test_proxy_fix_rejects_nul_in_x_forwarded_prefix():
    with pytest.raises(ValueError):
        await _run_proxy_fix(
            {"X-Forwarded-Prefix": "/api\x00evil"},
            x_prefix=1,
        )


def test_proxy_fix_chain_too_short_does_not_fabricate():
    """If the trust says 5 hops but only 1 is in the header, return None
    — never invent a client identity."""
    app = _make_app_with_proxy_fix(x_for=5)
    client = TestClient(app)
    resp = client.get("/info", headers={"X-Forwarded-For": "only-one"})
    assert resp.json()["client"] is None


# ── Quoted-delimiter handling (RFC 7239) ────────────────────────────────────


def test_parse_forwarded_quoted_comma_does_not_fake_hop():
    mw = ProxyFix(x_for=1, x_host=1)
    result = mw._parse_forwarded(
        'for=192.0.2.1; host="a,b"', x_for=1, x_proto=0, x_host=1, x_prefix=0
    )
    assert result == {"for": "192.0.2.1", "host": "a,b"}


def test_parse_forwarded_quoted_comma_multi_hop():
    mw = ProxyFix(x_for=1, x_host=1)
    # Two real hops; the rightmost (trusted) carries a quoted comma in host.
    result = mw._parse_forwarded(
        'for=10.0.0.1, for=192.0.2.1; host="a,b"', x_for=1, x_proto=0, x_host=1, x_prefix=0
    )
    assert result["for"] == "192.0.2.1"
    assert result["host"] == "a,b"


def test_forwarded_quoted_comma_integration():
    app = Veloce(openapi_url=None)
    app.add_middleware(ProxyFix(x_for=1, x_host=1))

    @app.get("/info")
    async def info(request):
        return {"client": request.client_host}

    client = TestClient(app)
    resp = client.get("/info", headers={"Forwarded": 'for=192.0.2.1; host="a,b"'})
    assert resp.json()["client"] == "192.0.2.1"
