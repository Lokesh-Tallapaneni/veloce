"""Trust reverse-proxy `X-Forwarded-*` / `Forwarded` headers.

When veloce sits behind a reverse proxy (nginx, Caddy, ALB, Cloudflare,
…), the immediate TCP peer is the proxy — not the original client. The
proxy injects headers describing the original request:

- `X-Forwarded-For: client, proxy1, proxy2`  (chain of upstream IPs)
- `X-Forwarded-Proto: https`                  (original scheme)
- `X-Forwarded-Host: example.com`             (original Host)
- `X-Forwarded-Port: 443`                     (original port)
- `X-Forwarded-Prefix: /api`                  (path prefix mounted)

RFC 7239 standardised these into a single `Forwarded` header
(`Forwarded: for=client; proto=https; host=example.com`). Both forms
are recognised here; `Forwarded` wins when present.

**Security:** trust only N hops upstream. A malicious client can spoof
these headers; only the proxies you control are trustworthy. The
`x_for=N` setting picks the Nth-from-the-right value, so if you have
two trusted proxies in front you set `x_for=2`.
"""

from __future__ import annotations

from veloce._constants import (
    HEADER_X_FORWARDED_FOR,
    HEADER_X_FORWARDED_HOST,
    HEADER_X_FORWARDED_PREFIX,
    HEADER_X_FORWARDED_PROTO,
)
from veloce._internal import _reject_header_crlf
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware


class ProxyFix(Middleware):
    """Reverse-proxy header trust middleware.

    Trusts N hops for each ``X-Forwarded-*`` header (right-to-left).
    Setting any field to ``0`` disables it. Negative values raise at
    construction.
    """

    def __init__(
        self,
        x_for: int = 1,
        x_proto: int = 1,
        x_host: int = 0,
        x_prefix: int = 0,
        trust_forwarded: bool = True,
    ) -> None:
        for name, val in (
            ("x_for", x_for),
            ("x_proto", x_proto),
            ("x_host", x_host),
            ("x_prefix", x_prefix),
        ):
            if val < 0:
                raise ValueError(f"{name} must be >= 0, got {val}")
        self.x_for = x_for
        self.x_proto = x_proto
        self.x_host = x_host
        self.x_prefix = x_prefix
        self.trust_forwarded = trust_forwarded

    async def process_request(self, request: Request) -> Response | None:
        """Rewrite request attributes from trusted proxy headers."""
        forwarded = request.headers.get("forwarded") if self.trust_forwarded else None
        fwd = (
            self._parse_forwarded(forwarded, self.x_for, self.x_proto, self.x_host, self.x_prefix)
            if forwarded
            else {}
        )

        # `Forwarded:` wins; else fall back to `X-Forwarded-*`.
        client = fwd.get("for") or self._pick_hop(
            request.headers.get("x-forwarded-for"), self.x_for
        )
        proto = fwd.get("proto") or self._pick_hop(
            request.headers.get("x-forwarded-proto"), self.x_proto
        )
        host = fwd.get("host") or self._pick_hop(
            request.headers.get("x-forwarded-host"), self.x_host
        )
        prefix = fwd.get("prefix") or self._pick_hop(
            request.headers.get("x-forwarded-prefix"), self.x_prefix
        )

        # Reject CR / LF / NUL in any trusted proxy value before it lands on
        # the request. These values flow into response headers (Location,
        # Set-Cookie, OpenAPI server URLs) via request.host / scheme /
        # script_root and would otherwise enable header injection.
        if client:
            _reject_header_crlf(client, HEADER_X_FORWARDED_FOR)
        if proto:
            _reject_header_crlf(proto, HEADER_X_FORWARDED_PROTO)
        if host:
            _reject_header_crlf(host, HEADER_X_FORWARDED_HOST)
        if prefix:
            _reject_header_crlf(prefix, HEADER_X_FORWARDED_PREFIX)

        if client:
            # Strip port suffix, but preserve IPv6 addresses.
            if "[" in client:
                # Bracketed IPv6, e.g. "[2001:db8::1]:8080" or "[2001:db8::1]"
                host_only = client.split("]", 1)[0][1:]
            elif client.count(":") >= 2:
                # Bare IPv6 (no brackets, no port), e.g. "2001:db8::1"
                host_only = client
            else:
                # IPv4 or hostname, optionally with ":port"
                host_only = client.split(":", 1)[0]
            request._state["proxy_fix_client"] = host_only
        if host:
            # Rewrite Host so URL.from_request picks up the original host.
            request.headers["host"] = host
        if proto:
            # Override scheme — `URL.from_request` now prefers `scope.scheme`
            # over `X-Forwarded-Proto`, so mutate both: the header for
            # downstream code that introspects it, and the scope so the
            # URL accessor agrees. This is the whole point of ProxyFix:
            # ASGI scope says "http" (TLS terminated upstream) but the
            # trusted hop tells us the original scheme was "https".
            request.headers["x-forwarded-proto"] = proto
            if isinstance(getattr(request, "scope", None), dict):
                request.scope["scheme"] = proto
        if prefix:
            request._state["proxy_fix_prefix"] = prefix

        # Invalidate the URL cache so subsequent accesses re-derive from
        # the now-corrected headers.
        request._url = None
        return None

    def _parse_forwarded(
        self, value: str, x_for: int, x_proto: int, x_host: int, x_prefix: int
    ) -> dict[str, str]:
        """Select trusted directives from a `Forwarded:` header (RFC 7239 §4).

        Each comma-separated element represents one hop. Attacker-controlled
        hops are on the LEFT; trusted proxies append on the RIGHT. For each
        directive (for, proto, host, prefix), select the element at
        position ``len(elements) - hop_count`` -- the same logic as
        ``_pick_hop``.
        """
        elements = [e.strip() for e in value.split(",") if e.strip()]
        parsed = [self._parse_forwarded_element(e) for e in elements]

        result: dict[str, str] = {}
        for directive, hops in (
            ("for", x_for),
            ("proto", x_proto),
            ("host", x_host),
            ("prefix", x_prefix),
        ):
            if hops <= 0 or len(parsed) < hops:
                continue
            val = parsed[-hops].get(directive)
            if val:
                result[directive] = val
        return result

    @staticmethod
    def _parse_forwarded_element(element: str) -> dict[str, str]:
        """Parse a single element of a `Forwarded:` header (RFC 7239 §4).

        Returns the lowercase key -> value mapping. Quotes and IPv6
        brackets are stripped.
        """
        result: dict[str, str] = {}
        for pair in element.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            k, _, v = pair.partition("=")
            k = k.strip().lower()
            v = v.strip().strip('"')
            if v.startswith("[") and "]" in v:
                v = v.split("]", 1)[0][1:]
            result[k] = v
        return result

    @staticmethod
    def _pick_hop(header_value: str | None, hops: int) -> str | None:
        """Pick the Nth-from-the-right value in a comma-separated header.

        `X-Forwarded-For: client, proxy1, proxy2` with hops=2 returns
        "client" (we trust two proxies); with hops=1 returns "proxy1".
        Returns None if `hops <= 0` or the list is shorter than `hops`.
        """
        if not header_value or hops <= 0:
            return None
        parts = [p.strip() for p in header_value.split(",") if p.strip()]
        if len(parts) < hops:
            return None
        return parts[-hops]
