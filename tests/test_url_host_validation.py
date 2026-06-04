"""Host-header validation in URL.from_request (RFC 3986 Sec. 3.2.2).

A malformed Host header must not poison host / netloc / base_url / url_root /
absolute-URL construction. An invalid Host falls back to the safe default
(`localhost`) rather than letting an injected path, query, or CRLF flow into
URLs the framework builds.
"""

from __future__ import annotations

from veloce import Request
from veloce.http.datastructures import URL, Headers


def _url(host: str, *, forwarded_port: int | None = None) -> URL:
    # Real callers hand `URL.from_request` a case-insensitive `Headers`
    # view; mirror that so the `Host` lookup resolves a lowercase key.
    return URL.from_request(
        Headers({"host": host}),
        path="/",
        query_string="",
        forwarded_port=forwarded_port,
    )


# ── Valid hosts pass through unchanged ────────────────────────────────


def test_plain_host_preserved():
    url = _url("example.com")
    assert url.host == "example.com"
    assert url.port is None


def test_host_with_port_preserved():
    url = _url("example.com:8080")
    assert url.host == "example.com"
    assert url.port == 8080


def test_subdomain_and_unreserved_chars_preserved():
    url = _url("api-v2.sub_domain.example.com")
    assert url.host == "api-v2.sub_domain.example.com"


def test_bracketed_ipv6_preserved():
    url = _url("[::1]:8443")
    assert url.host == "::1"
    assert url.port == 8443


def test_bracketed_ipv6_no_port_preserved():
    url = _url("[2001:db8::1]")
    assert url.host == "2001:db8::1"
    assert url.port is None


def test_bare_ipv6_preserved():
    url = _url("2001:db8::1")
    assert url.host == "2001:db8::1"
    assert url.port is None


# ── Malformed hosts fall back to the safe default ─────────────────────


def test_path_injected_host_rejected():
    """`evil.com/path?x` must not become the URL host."""
    url = _url("evil.com/path?x")
    assert url.host == "localhost"
    assert url.port is None


def test_query_injected_host_rejected():
    url = _url("evil.com?redirect=//attacker")
    assert url.host == "localhost"


def test_crlf_injected_host_rejected():
    url = _url("evil.com\r\nX-Injected: 1")
    assert url.host == "localhost"


def test_at_sign_userinfo_host_rejected():
    """An `@` would let `legit.com@evil.com` masquerade as legit.com."""
    url = _url("legit.com@evil.com")
    assert url.host == "localhost"


def test_whitespace_host_rejected():
    url = _url("evil .com")
    assert url.host == "localhost"


def test_non_numeric_port_rejected():
    url = _url("example.com:abc")
    assert url.host == "localhost"
    assert url.port is None


def test_out_of_range_port_rejected():
    url = _url("example.com:99999")
    assert url.host == "localhost"
    assert url.port is None


def test_empty_host_falls_back():
    url = _url("")
    assert url.host == "localhost"


def test_bracketed_ipv6_with_garbage_tail_rejected():
    url = _url("[::1]evil")
    assert url.host == "localhost"


def test_bracket_not_at_start_rejected():
    url = _url("evil[::1]")
    assert url.host == "localhost"


def test_ipv6_bad_chars_rejected():
    url = _url("[gggg::1]")
    assert url.host == "localhost"


# ── Downstream URL accessors do not leak the injected value ───────────


def test_base_url_not_poisoned_by_injected_host():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={"host": "evil.com/x?y"},
        body=b"",
    )
    assert req.base_url == "http://localhost"
    assert req.url_root == "http://localhost/"


def test_forwarded_port_still_applies_after_validation():
    """A valid host with no port still picks up a trusted forwarded port."""
    url = _url("example.com", forwarded_port=8443)
    assert url.host == "example.com"
    assert url.port == 8443


def test_host_port_wins_over_forwarded_port():
    url = _url("example.com:9000", forwarded_port=8443)
    assert url.port == 9000


def test_malformed_ipv6_host_falls_back():
    """A malformed IPv6 literal is rejected (falls back to localhost)."""
    assert _url("[:::1]:8080").host == "localhost"
    assert _url("[::::]").host == "localhost"
    assert _url("[2001:::1]").host == "localhost"


def test_valid_ipv6_host_preserved():
    """A syntactically valid IPv6 literal is preserved."""
    assert _url("[::1]:8080").host == "::1"
    assert _url("[2001:db8::1]").host == "2001:db8::1"
