"""HTTPS redirect middleware — upgrades plain-HTTP requests with a 308 (RFC 9110 Sec. 15.4.9)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import veloce.status as status
from veloce._constants import (
    HEADER_HOST,
    HEADER_X_FORWARDED_PROTO,
)
from veloce._protocol_constants import URL_SCHEME_HTTPS
from veloce.http.request import Request
from veloce.http.response import RedirectResponse, Response
from veloce.middleware.base import Middleware

if TYPE_CHECKING:  # pragma: no cover
    pass


# ── Transport upgrade ─────────────────────────────────────
class HTTPSRedirectMiddleware(Middleware):
    """Redirect HTTP requests to HTTPS.

    Resolves the request scheme in this order:
      1. ASGI scope `"scheme"` if set to `"https"`/`"wss"` (the
         server already terminated TLS).
      2. `X-Forwarded-Proto` header (when a `ProxyFix`-style middleware
         ran upstream this is already the trusted value).
      3. Default `http`.

    Uses 308 Permanent Redirect (RFC 9110 Sec. 15.4.9) so non-GET methods
    preserve their method and body; a `301` turns a `POST` into a `GET`.

    Pass `exempt_paths=("/health/", ...)` to serve some paths over plain HTTP
    (prefix match - use a trailing slash to scope to a segment). By default
    `/.well-known/acme-challenge/` is exempt (RFC 8555 Sec. 8.3: the HTTP-01
    challenge MUST be reachable over plain HTTP for certificate issuance and
    renewal); pass `exempt_acme_challenge=False` to drop that default.
    """

    def __init__(
        self,
        *,
        exempt_paths: tuple[str, ...] = (),
        exempt_acme_challenge: bool = True,
        name: str | None = None,
    ) -> None:
        """Configure the exemptions.

        Args:
            exempt_paths: Path prefixes served over plain HTTP.
            exempt_acme_challenge: Also exempt `/.well-known/acme-challenge/`.
            name: Name for `exclude_middleware`.

        """
        super().__init__(name=name)
        paths = list(exempt_paths)
        if exempt_acme_challenge:
            paths.append("/.well-known/acme-challenge/")
        # One `str.startswith` per request - no regex, no ReDoS surface.
        self._exempt_paths: tuple[str, ...] = tuple(paths)

    async def process_request(self, request: Request) -> Response | None:
        """Redirect HTTP requests to HTTPS with a 308 status."""
        # A replayed MCP call is not on the wire; there is nothing to upgrade.
        if request.is_mcp:
            return None
        # `is_secure` also probes the transport's TLS object, without which
        # natively-served TLS redirects to the URL it just answered - a loop.
        if request.is_secure:
            return None
        # Accepted only where nothing has judged it: once `ProxyFix` has run,
        # reading the raw header would accept a hop it refused and let a
        # TLS-stripping attacker suppress this redirect with one header.
        if "proxy_fix_applied" not in request._state:
            fwd_proto = request.headers.get(HEADER_X_FORWARDED_PROTO, "").lower()
            if fwd_proto == URL_SCHEME_HTTPS:
                return None

        # After the scheme short-circuits, so HTTPS traffic is never affected.
        if self._exempt_paths and request.path.startswith(self._exempt_paths):
            return None

        host = request.headers.get(HEADER_HOST, "localhost")
        url = f"{URL_SCHEME_HTTPS}://{host}{request.path}"
        if request.query_string:
            url += f"?{request.query_string}"
        return RedirectResponse(url, status_code=status.HTTP_308_PERMANENT_REDIRECT)
