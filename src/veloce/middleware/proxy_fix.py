"""Proxy fix — trust reverse-proxy `X-Forwarded-*` / `Forwarded` headers.

When veloce sits behind a reverse proxy (nginx, Caddy, ALB, Cloudflare,
...), the immediate TCP peer is the proxy - not the original client. The
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

    A proxy that appends its own line rather than rewriting the existing one
    leaves two field lines, and single-value access returns the *first* - the
    untrusted end of the chain. RFC 9110 Sec. 5.3 defines a repeatable field as
    the comma-joined list in order, which is exactly what the right-to-left hop
    math already expects.
    """
    # `getall` with a default, not `getlist`: the latter reaches its empty
    # result by catching `KeyError`, and raising one per absent header on every
    # request measured a third of this middleware's cost.
    values = headers.getall(name, ())
    if not values:
        return None
    if len(values) == 1:
        # The overwhelmingly common shape: no join, no allocation.
        first: str = values[0]
        return first
    return ", ".join(values)


class ProxyFix(Middleware):
    """Reverse-proxy header trust middleware.

    Trusts N hops for each ``X-Forwarded-*`` header (right-to-left).
    Setting any field to ``0`` disables it. Negative values raise at
    construction.

    ``x_port`` trusts ``X-Forwarded-Port``: the resolved port fills in the
    public port for ``request.url`` / redirects when the forwarded Host
    carries none, so a proxy on a non-default port (e.g. 8443) is preserved.
    An explicit port in the Host / ``X-Forwarded-Host`` always wins.

    ``trust_forwarded`` opts into RFC 7239 ``Forwarded``, which supersedes the
    ``X-Forwarded-*`` set and is then the sole authority for every directive it
    carries. Enable it only where every trusted proxy sets or sanitizes
    ``Forwarded`` itself: nginx, ALB and most CDNs emit ``X-Forwarded-*`` and
    leave ``Forwarded`` untouched, so a client-supplied header would otherwise
    decide the client address, scheme and host - and silence the header the
    proxy does control.

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
        # Forward the optional per-instance exclusion name to the base so
        # `add_middleware(ProxyFix, name="edge")` and route-level
        # `exclude_middleware=["edge"]` can target this instance.
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
        # `Forwarded` is the SOLE authority - a directive it does not carry
        # stays unset rather than falling through to the legacy header.
        # Mixing the two per directive was exploitable: a `Forwarded` chain
        # shorter than the configured trust depth correctly yields nothing, and
        # the fallback then handed that directive to whatever the client wrote
        # in `X-Forwarded-For`, which is precisely the value trust depth had
        # just refused. Failing closed here costs a deployment nothing, because
        # a proxy emitting `Forwarded` carries every directive in it.
        if forwarded:
            client = fwd.get("for")
            proto = fwd.get("proto")
            host = fwd.get("host")
        else:
            # Each lookup is gated on its own trust depth. `_pick_hop` returns
            # `None` for a depth of 0, so an ungated call still pays the header
            # lookup to reach a foregone answer - and `x_host`, `x_port` and
            # `x_prefix` are 0 by default, which is three lookups a stock
            # configuration cannot use.
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
        # Port and prefix keep their fallback, because RFC 7239 Sec. 4 defines
        # no directive for either: a `Forwarded` hop carries the public port
        # inside `host="example.com:8443"` (so it survives the Host splice
        # below via URL.from_request's own parse), and a path prefix has no
        # RFC spelling at all. `X-Forwarded-Port` / `X-Forwarded-Prefix` are
        # therefore the only channel a proxy has for them, and refusing those
        # would break real deployments to close nothing - unlike `for`,
        # `proto` and `host` above, where the RFC does define a directive and
        # the fallback let a refused hop through.
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

        # Reject CR / LF / NUL in any trusted proxy value before it lands on
        # the request. These values flow into response headers (Location,
        # Set-Cookie, OpenAPI server URLs) via request.host / scheme /
        # script_root and would otherwise enable header injection. Written out
        # per header rather than looped over a `(value, header)` sequence: this
        # middleware runs on every request through it, and building that
        # sequence would allocate a tuple per request to save five lines.
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
            # Strip the port suffix, preserving IPv6 literals. `lower=False`
            # keeps the client IP in its original casing - this is a stored
            # address, not a host compared case-insensitively.
            request._state["proxy_fix_client"] = _extract_host(client, lower=False)
        if host:
            # Rewrite Host so URL.from_request picks up the original host.
            request.headers["host"] = host
        if port:
            # Stash the trusted public port as an int. URL.from_request uses
            # it only when the (possibly rewritten) Host header carries no
            # port of its own, so an explicit `host:port` always wins. A
            # non-numeric or out-of-range value is dropped rather than trusted.
            try:
                port_num = int(port)
            except ValueError:
                port_num = -1
            if 0 < port_num <= 65535:
                request._state["proxy_fix_port"] = port_num
        if proto:
            # Override scheme - `URL.from_request` now prefers `scope.scheme`
            # over `X-Forwarded-Proto`, so mutate both: the header for
            # downstream code that introspects it, and the scope so the
            # URL accessor agrees. This is the whole point of ProxyFix:
            # ASGI scope says "http" (TLS terminated upstream) but the
            # trusted hop tells us the original scheme was "https".
            request.headers[HEADER_X_FORWARDED_PROTO] = proto
            # Normalise on the way in. RFC 7239 Sec. 4 makes the `proto`
            # directive case-insensitive and RFC 3986 Sec. 3.1 says the same of
            # a URI scheme, so a proxy spelling `HTTPS` names the same scheme -
            # but written verbatim it reached every consumer that compares the
            # raw scope value, and an uppercase hop made a guard read an
            # encrypted connection as cleartext.
            proto = proto.lower()
            # `Request.scope` is a framework-owned field always set in __init__
            # (to `scope or {}`), so access it directly; only the dict shape is
            # checked before mutating the scheme key.
            if isinstance(request.scope, dict):
                request.scope["scheme"] = proto
        if prefix:
            request._state["proxy_fix_prefix"] = prefix
        # Mark that trust depth has been applied to this request. `URL.from_request`
        # consults `X-Forwarded-Proto` directly when nothing else supplies a
        # scheme, which is a reasonable default with no ProxyFix installed - but
        # once ProxyFix HAS run, that fallback would hand the scheme to a hop
        # ProxyFix just refused. Setting this stands the fallback down, so only
        # the value written into the scope above counts.
        request._state["proxy_fix_applied"] = True

        # Invalidate the URL cache so subsequent accesses re-derive from
        # the now-corrected headers.
        request._url = None
        return None

    def _parse_forwarded(
        self, value: str, x_for: int, x_proto: int, x_host: int, x_prefix: int
    ) -> dict[str, str]:
        """Select trusted directives from a `Forwarded:` header (RFC 7239 Sec. 4).

        Each comma-separated element represents one hop. Attacker-controlled
        hops are on the LEFT; trusted proxies append on the RIGHT. For each
        directive (for, proto, host, prefix), select the element at
        position ``len(elements) - hop_count`` -- the same logic as
        ``_pick_hop``.
        """
        # Split on commas OUTSIDE quoted strings so a quoted comma in a
        # directive value (e.g. `host="a,b"`) does not fake an extra hop.
        raw, unterminated = split_outside_quotes_checked(value, ",")
        if unterminated:
            # A quoted comma is only legal inside a properly closed
            # quoted-string (RFC 7239 Sec. 4, RFC 9110 Sec. 5.6.4). Honouring an
            # unbalanced `"` would let the sender put every comma its trusted
            # proxies later append inside one quoted region, collapsing the hop
            # count to whatever element it chose. Trust nothing from it.
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

        Returns the lowercase key -> value mapping with quotes stripped. For
        the `for` / `by` node identifiers the bracketed-IPv6 wrapper is removed
        to expose the bare address (RFC 7239 Sec. 6); the `host` directive is
        an authority (`host[:port]`, RFC 7239 Sec. 5.3) and is kept verbatim so
        a bracketed IPv6 host with a port survives intact when spliced into the
        Host header.
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
        may have been supplied by the client.

        `X-Forwarded-For: client, proxy1, proxy2` returns "proxy2" with hops=1,
        "proxy1" with hops=2 and "client" with hops=3 - so `hops` is the number
        of proxies in front of the application, and the value returned is the
        address the outermost trusted one saw. Returns None if `hops <= 0` or
        the list is shorter than `hops`, because there is then no entry that
        trust reaches.
        """
        if not header_value or hops <= 0:
            return None
        parts = [p.strip() for p in header_value.split(",") if p.strip()]
        if len(parts) < hops:
            return None
        return parts[-hops]
