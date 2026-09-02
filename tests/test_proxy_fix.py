"""ProxyFix middleware tests."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import ProxyFix, Request, Veloce
from veloce.testclient import TestClient


def _req(headers: dict[str, str]) -> Request:
    return make_request(method="GET", path="/", query_string="", headers=headers, body=b"")


# ── Hop-picking ────────────────────────────────────────────────────────


# `_pick_hop` is a pure function over one header value, and the cases below are
# its edges - a chain shorter than the trust depth, a zero depth, a missing
# header. The wiring that calls it is covered end to end further down; these
# are the inputs that are awkward to arrange through a client and cheap to
# state directly.


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


# These drove `_parse_forwarded` directly, passing `x_for=`/`x_proto=`/`x_host=`
# to the *call* after already passing them to the constructor - so the two could
# disagree and the test would still pass, while the instance under test decided
# nothing. Through the middleware the constructor's configuration is what
# selects, which is also where the security property lives: the wiring that
# picks the trusted hop is the part an attacker attacks.


def test_a_forwarded_header_supplies_client_scheme_and_host():
    app = _make_app_with_proxy_fix(x_for=1, x_proto=1, x_host=1, trust_forwarded=True)
    body = (
        TestClient(app)
        .get("/info", headers={"Forwarded": "for=client.example.com; proto=https; host=ex.com"})
        .json()
    )
    assert body["client"] == "client.example.com"
    assert body["scheme"] == "https"
    assert body["host"] == "ex.com"


def test_a_forwarded_for_drops_the_ipv6_brackets():
    """`for=` carries a node identifier; the address is what a handler wants."""
    app = _make_app_with_proxy_fix(x_for=1, trust_forwarded=True)
    body = TestClient(app).get("/info", headers={"Forwarded": 'for="[2001:db8::1]:8080"'}).json()
    assert body["client"] == "2001:db8::1"


def test_a_forwarded_host_authority_survives_with_its_port():
    """A `host=` authority is kept whole; only `for=`/`by=` get unbracketed.

    Through the middleware the observable is what a handler reads, and the URL
    layer splits the authority: the brackets belong to the wire form, while the
    host and the port are what `request.url` exposes. The unit test this
    replaced asserted the intermediate string and so could not tell whether
    anything downstream understood it.
    """
    app = _make_app_with_proxy_fix(x_host=1, trust_forwarded=True)
    body = TestClient(app).get("/info", headers={"Forwarded": 'host="[2001:db8::1]:8443"'}).json()
    assert body["host"] == "2001:db8::1"
    assert body["port"] == 8443


def test_a_forwarded_chain_is_selected_from_the_right():
    """Multiple proxies: select by trust depth from the right, not the left.

    The left of the chain is attacker-supplied. Picking from it is the classic
    spoof, and only the wired middleware can show that the configured depth is
    the one applied.
    """
    app = _make_app_with_proxy_fix(x_for=1, x_proto=1, trust_forwarded=True)
    body = (
        TestClient(app)
        .get(
            "/info",
            headers={"Forwarded": "for=attacker; proto=http, for=trusted; proto=https"},
        )
        .json()
    )
    assert body["client"] == "trusted"
    assert body["scheme"] == "https"


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
            "port": request.url.port,
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
    # `x_for=1` trusts one hop, counted from the right: `_pick_hop` returns the
    # rightmost value, `10.0.0.1`. Reaching the original client `203.0.113.1`
    # through two proxies needs `x_for=2`.
    #
    # Selecting from the right is the security property, not a detail: the left
    # of `X-Forwarded-For` is attacker-controlled, so trust is only ever counted
    # back from the peer Veloce actually spoke to (RFC 7239 Sec. 5.2).
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
    # No proxy_fix_client was stored, so the reported address is the peer that
    # actually connected - never the value the untrusted header asked for.
    assert resp.json()["client"] != "evil.example.com"


def test_parse_forwarded_extracts_prefix_extension():
    """RFC 7239 §4 allows extension fields; `prefix=` is a de-facto convention."""
    # Depths are passed to the call only: giving them to the constructor as
    # well let the two disagree while the test still passed, which is the
    # pattern the section comment above records as removed.
    pf = ProxyFix()
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
    # The short chain is refused, so the single forwarded entry is never
    # promoted to the client identity; the real peer is reported instead.
    assert resp.json()["client"] != "only-one"


# ── Quoted-delimiter handling (RFC 7239) ────────────────────────────────────


def test_parse_forwarded_quoted_comma_does_not_fake_hop():
    mw = ProxyFix()
    result = mw._parse_forwarded(
        'for=192.0.2.1; host="a,b"', x_for=1, x_proto=0, x_host=1, x_prefix=0
    )
    assert result == {"for": "192.0.2.1", "host": "a,b"}


def test_parse_forwarded_quoted_comma_multi_hop():
    mw = ProxyFix()
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


# ── X-Forwarded-Port (M10) ─────────────────────────────────────────────


def _make_app_capturing_url(**kwargs) -> Veloce:
    app = Veloce(debug=True, openapi_url=None)
    app.add_middleware(ProxyFix(**kwargs))

    @app.get("/info")
    async def info(request: Request):
        url = request.url
        return {"netloc": url.netloc, "port": url.port, "base_url": request.base_url}

    return app


def test_construction_rejects_negative_port():
    with pytest.raises(ValueError):
        ProxyFix(x_port=-1)


def test_x_forwarded_port_fills_in_non_default_port():
    """A forwarded Host without a port plus X-Forwarded-Port keeps the port."""
    app = _make_app_capturing_url(x_host=1, x_port=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"X-Forwarded-Host": "public.example.com", "X-Forwarded-Port": "8443"},
    )
    body = resp.json()
    assert body["port"] == 8443
    assert body["netloc"] == "public.example.com:8443"
    assert body["base_url"] == "http://public.example.com:8443"


def test_x_forwarded_port_omitted_when_scheme_default():
    """Port 443 under https equals the scheme default and is hidden in netloc."""
    app = _make_app_capturing_url(x_host=1, x_port=1, x_proto=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={
            "X-Forwarded-Host": "public.example.com",
            "X-Forwarded-Port": "443",
            "X-Forwarded-Proto": "https",
        },
    )
    body = resp.json()
    assert body["port"] == 443
    assert body["netloc"] == "public.example.com"


def test_explicit_host_port_wins_over_x_forwarded_port():
    """A port in X-Forwarded-Host beats a separate X-Forwarded-Port."""
    app = _make_app_capturing_url(x_host=1, x_port=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={
            "X-Forwarded-Host": "public.example.com:9000",
            "X-Forwarded-Port": "8443",
        },
    )
    assert resp.json()["port"] == 9000


def test_x_forwarded_port_disabled_by_default():
    """Without x_port the header is ignored - no port leaks into the URL."""
    app = _make_app_capturing_url(x_host=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"X-Forwarded-Host": "public.example.com", "X-Forwarded-Port": "8443"},
    )
    assert resp.json()["port"] is None


def test_x_forwarded_port_rejects_non_numeric():
    """A non-numeric forwarded port is dropped rather than trusted."""
    app = _make_app_capturing_url(x_host=1, x_port=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"X-Forwarded-Host": "public.example.com", "X-Forwarded-Port": "notaport"},
    )
    assert resp.json()["port"] is None


def test_x_forwarded_port_rejects_out_of_range():
    app = _make_app_capturing_url(x_host=1, x_port=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"X-Forwarded-Host": "public.example.com", "X-Forwarded-Port": "70000"},
    )
    assert resp.json()["port"] is None


def test_x_forwarded_port_walks_back_with_trust_depth():
    """`X-Forwarded-Port: 8443, 80` + x_port=2 trusts the outer (8443)."""
    app = _make_app_capturing_url(x_host=1, x_port=2)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={
            "X-Forwarded-Host": "public.example.com",
            "X-Forwarded-Port": "8443, 80",
        },
    )
    assert resp.json()["port"] == 8443


async def test_proxy_fix_rejects_crlf_in_x_forwarded_port():
    with pytest.raises(ValueError):
        await _run_proxy_fix(
            {"X-Forwarded-Port": "8443\r\nInjected: 1"},
            x_port=1,
        )


def test_forwarded_host_with_port_survives():
    """RFC 7239 carries the port inside `host=...:port`; it reaches the URL."""
    app = _make_app_capturing_url(x_host=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"Forwarded": "for=192.0.2.1; host=public.example.com:8443"},
    )
    assert resp.json()["port"] == 8443


def test_forwarded_ipv6_host_with_port_survives():
    """A bracketed IPv6 `host=` authority reaches the URL with brackets+port."""
    app = _make_app_capturing_url(x_host=1)
    client = TestClient(app)
    resp = client.get(
        "/info",
        headers={"Forwarded": 'for=192.0.2.1; host="[2001:db8::1]:8443"'},
    )
    body = resp.json()
    assert body["netloc"] == "[2001:db8::1]:8443"
    assert body["port"] == 8443


# ── a malformed quote must not shrink the hop count ──────────────────


def test_an_unterminated_quote_in_forwarded_is_not_trusted():
    """NEGATIVE: one `"` must not collapse the hop count.

    The trusted proxies append after the attacker's element, so an unclosed
    quote puts every comma they append inside one quoted region and the
    element count collapses - `_pick_hop` then selects the attacker's element.
    Control and attack differ by exactly that one character.
    """
    proxy_fix = ProxyFix(x_for=2, x_proto=2, x_host=2)
    header = 'proto=https;host=evil.example.net;for=1.2.3.4, x=", for=198.51.100.7, for=203.0.113.9'

    assert proxy_fix._parse_forwarded(header, 2, 2, 2, 0) == {}


def test_an_ordinary_forwarded_chain_still_picks_the_right_hop():
    """POSITIVE: the control case - the same header without the quote."""
    proxy_fix = ProxyFix(x_for=2, x_proto=2, x_host=2)
    header = "proto=https;host=evil.example.net;for=1.2.3.4, x=, for=198.51.100.7, for=203.0.113.9"

    assert proxy_fix._parse_forwarded(header, 2, 2, 2, 0)["for"] == "198.51.100.7"


def test_a_properly_quoted_comma_still_does_not_fake_a_hop():
    """POSITIVE: failing closed on a malformed quote must not reject a legal one.

    A quoted comma inside a closed quoted-string is exactly what
    `split_outside_quotes` exists to handle, and must still be one element.
    """
    proxy_fix = ProxyFix(x_for=1, x_proto=1, x_host=1)
    header = 'host="a,b";for=198.51.100.7'

    assert proxy_fix._parse_forwarded(header, 1, 1, 1, 0)["for"] == "198.51.100.7"


# ── a repeated hop header must be read whole, in received order ──────


async def test_a_second_x_forwarded_for_line_is_not_discarded():
    """NEGATIVE: the proxy appends a line; reading only the first trusts the client.

    `Headers.get` returns the first occurrence, so the attacker's line won and
    the real peer never appeared in the chain at all. A proxy that appends
    rather than rewrites (HAProxy `option forwardfor` without `if-none`)
    produces exactly this shape.

    Built as a `Request` rather than driven through `TestClient`: the client
    collapses repeated header pairs into one line, so a test written that way
    passes whether or not the fix is present.
    """
    request = Request(
        method="GET",
        path="/",
        query_string="",
        headers=[
            ("host", "app.example.com"),
            ("x-forwarded-for", "9.9.9.9"),
            ("x-forwarded-for", "203.0.113.9"),
        ],
        body=b"",
    )
    assert request.headers.getlist("x-forwarded-for") == ["9.9.9.9", "203.0.113.9"]

    await ProxyFix(x_for=1).process_request(request)

    assert request.remote_addr == "203.0.113.9"


async def test_a_second_forwarded_line_is_not_discarded():
    """NEGATIVE: the same shape on the RFC 7239 header."""
    request = Request(
        method="GET",
        path="/",
        query_string="",
        headers=[
            ("host", "app.example.com"),
            ("forwarded", "for=1.2.3.4;proto=https;host=evil.example.net"),
            ("forwarded", "for=203.0.113.9"),
        ],
        body=b"",
    )

    await ProxyFix(x_for=1, x_proto=1, x_host=1).process_request(request)

    assert request.remote_addr == "203.0.113.9"
    assert request.scheme != "https"


async def test_a_single_line_chain_is_unchanged():
    """POSITIVE: the ordinary single-header case must behave exactly as before."""
    request = Request(
        method="GET",
        path="/",
        query_string="",
        headers=[("host", "app.example.com"), ("x-forwarded-for", "9.9.9.9, 203.0.113.9")],
        body=b"",
    )

    await ProxyFix(x_for=1).process_request(request)

    assert request.remote_addr == "203.0.113.9"


def test_joining_preserves_received_order():
    """POSITIVE: the trusted end is the right end, so order must not move."""
    from veloce.http.datastructures import Headers
    from veloce.middleware.proxy_fix import _hop_header

    headers = Headers([("x-forwarded-for", "a"), ("x-forwarded-for", "b")])
    assert _hop_header(headers, "x-forwarded-for") == "a, b"


def test_an_absent_hop_header_is_still_none():
    """POSITIVE: absence must stay distinguishable from an empty value."""
    from veloce.http.datastructures import Headers
    from veloce.middleware.proxy_fix import _hop_header

    assert _hop_header(Headers([]), "x-forwarded-for") is None
