"""Proxy fix — trust reverse-proxy `X-Forwarded-*` / `Forwarded` headers.

Behind a reverse proxy the immediate TCP peer is the proxy, and the headers
describing the original request are:

- `X-Forwarded-For: client, proxy1, proxy2`  (chain of upstream IPs)
- `X-Forwarded-Proto: https`                  (original scheme)
- `X-Forwarded-Host: example.com`             (original Host)
- `X-Forwarded-Port: 443`                     (original port)
- `X-Forwarded-Prefix: /api`                  (path prefix mounted)

RFC 7239 standardised these into a single `Forwarded` header. Both forms are
recognised here; `Forwarded` wins when present.

**Security:** a malicious client can spoof all of them, so only the proxies you
control are trustworthy. `x_for=N` picks the Nth-from-the-right value, so two
trusted proxies in front means `x_for=2`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from veloce._constants import (
    HEADER_X_FORWARDED_FOR,
    HEADER_X_FORWARDED_HOST,
    HEADER_X_FORWARDED_PORT,
    HEADER_X_FORWARDED_PREFIX,
    HEADER_X_FORWARDED_PROTO,
)
from veloce._header_parsing import (
    split_outside_quotes,
    split_outside_quotes_checked,
    unquote_value,
)
from veloce._internal import _extract_host, _reject_header_crlf
from veloce.http.request import Request
from veloce.http.response import Response
from veloce.middleware.base import Middleware

if TYPE_CHECKING:  # pragma: no cover
    from veloce.http.datastructures import Headers


def _hop_header(headers: Headers, name: str) -> str | None:
    """Return every occurrence of `name`, comma-joined in received order.

    Single-value access would return the *first* field line, the untrusted end
    of the chain. RFC 9110 Sec. 5.3 defines a repeatable field as the
    comma-joined list in order, which the right-to-left hop math expects.
    """
    # `getall` with a default, not `getlist`: the latter raises `KeyError` per
    # absent header, which measured a third of this middleware's cost.
    values = headers.getall(name, ())
    if not values:
        return None
    if len(values) == 1:
        # The common shape: no join, no allocation.
        first: str = values[0]
        return first
    return ", ".join(values)


class ProxyFix(Middleware):
    """Reverse-proxy header trust middleware.

    Trusts N hops for each ``X-Forwarded-*`` header (right-to-left). ``0``
    disables a field; a negative value raises at construction.

    ``x_port`` fills the public port for ``request.url`` / redirects when the
    forwarded Host carries none; an explicit port in the Host always wins.

    ``trust_forwarded`` opts into RFC 7239 ``Forwarded``, which then supersedes
    the ``X-Forwarded-*`` set entirely. Enable it only where every trusted proxy
    sets or sanitizes ``Forwarded`` itself: nginx, ALB and most CDNs leave it
    untouched, so a client-supplied header would otherwise decide the client
    address, scheme and host.

    Usage::

        # Behind two trusted proxies forwarding client IP and scheme.
        app.add_middleware(ProxyFix(x_for=2, x_proto=1, x_host=1))
    """

    def __init__(
        self,
        x_for: int = 1,
        x_proto: int = 1,
        x_host: int = 0,
        x_port: int = 0,
        x_prefix: int = 0,
        trust_forwarded: bool = False,
        name: str | None = None,
    ) -> None:
        # The per-instance name lets `exclude_middleware=[...]` target it.
        super().__init__(name=name)
        for field, val in (
            ("x_for", x_for),
            ("x_proto", x_proto),
            ("x_host", x_host),
            ("x_port", x_port),
            ("x_prefix", x_prefix),
        ):
            if val < 0:
                raise ValueError(f"{field} must be >= 0, got {val}")
        self.x_for = x_for
        self.x_proto = x_proto
        self.x_host = x_host
        self.x_port = x_port
        self.x_prefix = x_prefix
        self.trust_forwarded = trust_forwarded

    async def process_request(self, request: Request) -> Response | None:
        """Rewrite request attributes from trusted proxy headers."""
        forwarded = _hop_header(request.headers, "forwarded") if self.trust_forwarded else None
        fwd = (
            self._parse_forwarded(forwarded, self.x_for, self.x_proto, self.x_host, self.x_prefix)
            if forwarded
            else {}
        )

        # RFC 7239 Sec. 4 supersedes the `X-Forwarded-*` set, so a trusted
        # `Forwarded` is the SOLE authority: mixing per directive means a chain
        # shorter than the trust depth falls through to the legacy header,
        # handing the attacker back the hop trust depth just refused.
        if forwarded:
            client = fwd.get("for")
            proto = fwd.get("proto")
            host = fwd.get("host")
        else:
            # Gated on trust depth: `x_host`, `x_port` and `x_prefix` are 0 by
            # default, so an ungated call is three header lookups for nothing.
            client = (
                self._pick_hop(_hop_header(request.headers, HEADER_X_FORWARDED_FOR), self.x_for)
                if self.x_for
                else None
            )
            proto = (
                self._pick_hop(_hop_header(request.headers, HEADER_X_FORWARDED_PROTO), self.x_proto)
                if self.x_proto
                else None
            )
            host = (
                self._pick_hop(_hop_header(request.headers, HEADER_X_FORWARDED_HOST), self.x_host)
                if self.x_host
                else None
            )
        # RFC 7239 Sec. 4 defines no directive for port or prefix (a hop carries
        # the port inside `host="example.com:8443"`), so the legacy headers are
        # the only channel for them and keep their fallback.
        port = (
            self._pick_hop(_hop_header(request.headers, HEADER_X_FORWARDED_PORT), self.x_port)
            if self.x_port
            else None
        )
        prefix = fwd.get("prefix") or (
            self._pick_hop(_hop_header(request.headers, HEADER_X_FORWARDED_PREFIX), self.x_prefix)
            if self.x_prefix
            else None
        )

        # These values reach response headers (Location, Set-Cookie, OpenAPI
        # server URLs), so CR / LF / NUL in one is header injection. Unrolled
        # rather than looped, to avoid a per-request tuple.
        if client:
            _reject_header_crlf(client, HEADER_X_FORWARDED_FOR)
        if proto:
            _reject_header_crlf(proto, HEADER_X_FORWARDED_PROTO)
        if host:
            _reject_header_crlf(host, HEADER_X_FORWARDED_HOST)
        if port:
            _reject_header_crlf(port, HEADER_X_FORWARDED_PORT)
        if prefix:
            _reject_header_crlf(prefix, HEADER_X_FORWARDED_PREFIX)

        if client:
            # `lower=False`: a stored address, not a case-insensitive host.
            request._state["proxy_fix_client"] = _extract_host(client, lower=False)
        if host:
            # Rewrite Host so URL.from_request picks up the original host.
            request.headers["host"] = host
        if port:
            # Consulted only when the Host header carries no port of its own.
            # A non-numeric or out-of-range value is dropped, not trusted.
            try:
                port_num = int(port)
            except ValueError:
                port_num = -1
            if 0 < port_num <= 65535:
                request._state["proxy_fix_port"] = port_num
        if proto:
            # `URL.from_request` prefers `scope.scheme`, so mutate both: the
            # header for code that introspects it, the scope for the accessor.
            request.headers[HEADER_X_FORWARDED_PROTO] = proto
            # RFC 7239 Sec. 4 and RFC 3986 Sec. 3.1 both make the scheme
            # case-insensitive, so `HTTPS` verbatim would read as cleartext to
            # every consumer comparing the raw scope value.
            proto = proto.lower()
            if isinstance(request.scope, dict):
                request.scope["scheme"] = proto
        if prefix:
            request._state["proxy_fix_prefix"] = prefix
        # Stands down the `X-Forwarded-Proto` fallback in `URL.from_request`,
        # which would otherwise hand the scheme to a hop trust depth refused.
        request._state["proxy_fix_applied"] = True

        # Re-derive the URL from the corrected headers.
        request._url = None
        return None

    def _parse_forwarded(
        self, value: str, x_for: int, x_proto: int, x_host: int, x_prefix: int
    ) -> dict[str, str]:
        """Select trusted directives from a `Forwarded:` header (RFC 7239 Sec. 4).

        Each comma-separated element is one hop. Attacker-controlled hops are on
        the LEFT and trusted proxies append on the RIGHT, so every directive is
        read at ``len(elements) - hop_count``, as in ``_pick_hop``.
        """
        # Split on commas OUTSIDE quoted strings so a quoted comma in a
        # directive value (e.g. `host="a,b"`) does not fake an extra hop.
        raw, unterminated = split_outside_quotes_checked(value, ",")
        if unterminated:
            # RFC 7239 Sec. 4 / RFC 9110 Sec. 5.6.4 allow a quoted comma only
            # in a closed quoted-string: an unbalanced `"` would swallow the
            # commas trusted proxies append, collapsing the hop count.
            return {}
        elements = [e for e in raw if e.strip()]
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
        """Parse a single element of a `Forwarded:` header (RFC 7239 Sec. 4).

        Returns the lowercase key -> value mapping with quotes stripped. `for` /
        `by` are node identifiers, so the bracketed-IPv6 wrapper is stripped
        (RFC 7239 Sec. 6); `host` is an authority (RFC 7239 Sec. 5.3) and stays
        verbatim so a bracketed IPv6 host with a port survives.
        """
        result: dict[str, str] = {}
        for pair in split_outside_quotes(element, ";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            k, _, v = pair.partition("=")
            k = k.strip().lower()
            v = unquote_value(v)
            if k in ("for", "by") and v.startswith("[") and "]" in v:
                v = v.split("]", 1)[0][1:]
            result[k] = v
        return result

    @staticmethod
    def _pick_hop(header_value: str | None, hops: int) -> str | None:
        """Pick the Nth-from-the-right value in a comma-separated header.

        Counting from the right is what makes the choice safe: the rightmost
        entry is the one the closest proxy appended, and everything to its left
        may have been supplied by the client. `X-Forwarded-For: client, proxy1,
        proxy2` returns "proxy2" with hops=1 and "client" with hops=3. None when
        `hops <= 0` or the list is shorter than `hops`, because trust then
        reaches no entry.
        """
        if not header_value or hops <= 0:
            return None
        parts = [p.strip() for p in header_value.split(",") if p.strip()]
        if len(parts) < hops:
            return None
        return parts[-hops]
