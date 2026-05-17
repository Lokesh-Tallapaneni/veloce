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

from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware


class ProxyFix(Middleware):
    """Reverse-proxy header trust middleware.

    Args:
        x_for:    trust this many hops in `X-Forwarded-For` (right-to-left).
        x_proto:  same for `X-Forwarded-Proto`.
        x_host:   same for `X-Forwarded-Host`.
        x_port:   same for `X-Forwarded-Port`.
        x_prefix: same for `X-Forwarded-Prefix`.
        trust_forwarded: if True, parse RFC 7239 `Forwarded:` first; fall
            back to `X-Forwarded-*` if absent. Default True.

    Setting any field to `0` disables it. Negative values raise at
    construction.
    """

    def __init__(
        self,
        x_for: int = 1,
        x_proto: int = 1,
        x_host: int = 0,
        x_port: int = 0,
        x_prefix: int = 0,
        trust_forwarded: bool = True,
    ) -> None:
        for name, val in (
            ("x_for", x_for),
            ("x_proto", x_proto),
            ("x_host", x_host),
            ("x_port", x_port),
            ("x_prefix", x_prefix),
        ):
            if val < 0:
                raise ValueError(f"{name} must be >= 0, got {val}")
        self.x_for = x_for
        self.x_proto = x_proto
        self.x_host = x_host
        self.x_port = x_port
        self.x_prefix = x_prefix
        self.trust_forwarded = trust_forwarded

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

    @staticmethod
    def _parse_forwarded(value: str) -> dict[str, str]:
        """Parse one element of a `Forwarded:` header (RFC 7239 §4).

        Returns the lowercase key → value mapping for the first forwarded
        element (the closest upstream). Quotes and IPv6 brackets stripped.
        """
        result: dict[str, str] = {}
        first = value.split(",", 1)[0]
        for pair in first.split(";"):
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

    async def process_request(self, request: Request) -> Response | None:
        forwarded = request.headers.get("forwarded") if self.trust_forwarded else None
        fwd = self._parse_forwarded(forwarded) if forwarded else {}

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
        port_val = self._pick_hop(request.headers.get("x-forwarded-port"), self.x_port)
        prefix = self._pick_hop(request.headers.get("x-forwarded-prefix"), self.x_prefix)

        if client:
            # `X-Forwarded-For` values may include `:port`; keep only the host.
            host_only = client.split(":", 1)[0] if ":" in client else client
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
        if port_val:
            request._state["proxy_fix_port"] = port_val
        if prefix:
            request._state["proxy_fix_prefix"] = prefix

        # Invalidate the URL cache so subsequent accesses re-derive from
        # the now-corrected headers.
        request._url = None
        return None
